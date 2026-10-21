// Phone acquisition client. Captures camera frames + gyro/accel (+ optional GNSS),
// timestamps everything with ONE monotonic clock (performance.now), and streams it
// to the PC over a single WebSocket. No inference runs on the phone.
// Wire format: see pc_app/protocol.py and docs/protocol.md.
'use strict';

const $ = (id) => document.getElementById(id);
const PROTOCOL_VERSION = 1;
const nowNs = () => Math.round(performance.now() * 1e6); // exact in a double for ~104 days
const SESSION_ID = randomId(); // one page load = one phone clock domain

let cfg = { width: 1280, height: 720, fps: 30, jpeg_quality: 0.8, imu_hz: 100, gnss: false,
            max_buffered_bytes: 1500000 };
let ws = null, running = false, recording = false;
let stream = null, video = $('preview'), canvas = null, ctx = null;
let sensors = [], imuApi = 'none', geoWatch = null, wakeLock = null;
let frameIndex = 0, lastFrameT = -1e12, encoding = false, rvfc = false;
const seq = { gyro: 0, accel: 0, gnss: 0 };
let imuQueue = [], gnssQueue = [];
const counters = { frames_captured: 0, frames_sent: 0, frames_dropped_busy: 0,
                   frames_dropped_backpressure: 0, imu_samples: 0 };
const tsSourceCount = { 1: 0, 2: 0, 3: 0 };
let timers = [];

function randomId() {
  const a = new Uint8Array(4);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
}

function orientationDeg() {
  const a = (screen.orientation && typeof screen.orientation.angle === 'number')
    ? screen.orientation.angle : (window.orientation || 0);
  return ((a % 360) + 360) % 360;
}

// ------------------------------------------------------------------ settings UI
async function loadConfig() {
  try {
    const r = await fetch('config.json', { cache: 'no-store' });
    if (r.ok) cfg = { ...cfg, ...(await r.json()) };
  } catch (e) { /* defaults */ }
  const res = `${cfg.width}x${cfg.height}`;
  if (![...$('res').options].some((o) => o.value === res)) $('res').add(new Option(res, res));
  $('res').value = res;
  $('fps').value = String(cfg.fps);
  $('quality').value = String(cfg.jpeg_quality);
  $('imuhz').value = String(cfg.imu_hz);
  $('gnss').checked = !!cfg.gnss;
  showQuality();
}
function showQuality() { $('quality-v').textContent = Number($('quality').value).toFixed(2); }
function readSettings() {
  const [w, h] = $('res').value.split('x').map(Number);
  cfg.width = w; cfg.height = h;
  cfg.fps = Number($('fps').value);
  cfg.jpeg_quality = Number($('quality').value);
  cfg.imu_hz = Number($('imuhz').value);
  cfg.gnss = $('gnss').checked;
}

// ------------------------------------------------------------------ connection
function wsUrl() {
  // Over a public tunnel the PC requires the access key from the link it printed (?k=...).
  const k = new URLSearchParams(location.search).get('k');
  return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/phone' +
    (k ? '?k=' + encodeURIComponent(k) : '');
}

function hello() {
  const track = stream && stream.getVideoTracks()[0];
  return {
    type: 'hello', protocol: PROTOCOL_VERSION, session_id: SESSION_ID,
    user_agent: navigator.userAgent, platform: navigator.platform || '',
    time_origin_ms: performance.timeOrigin, cross_origin_isolated: !!self.crossOriginIsolated,
    clock: 'performance.now', imu_api: imuApi, imu_hz_requested: cfg.imu_hz,
    frame_timestamp: rvfc ? 'requestVideoFrameCallback' : 'poll',
    video: { requested: { width: cfg.width, height: cfg.height, fps: cfg.fps }, jpeg_quality: cfg.jpeg_quality,
             track_settings: track ? track.getSettings() : null, label: track ? track.label : null },
    orientation_deg: orientationDeg(),
    screen: { w: screen.width, h: screen.height, dpr: window.devicePixelRatio },
  };
}

function connect() {
  ws = new WebSocket(wsUrl());
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { ws.send(JSON.stringify(hello())); setConn(true); };
  ws.onmessage = (ev) => {
    const t = nowNs(); // read the clock before anything else for clock sync
    const m = JSON.parse(ev.data);
    if (m.type === 'ping') ws.send(JSON.stringify({ type: 'pong', id: m.id, t }));
    else if (m.type === 'state' || m.type === 'welcome') setRec(!!m.recording);
  };
  ws.onclose = () => { setConn(false); if (running) setTimeout(connect, 1000); };
  ws.onerror = () => {};
}
const wsOpen = () => ws && ws.readyState === WebSocket.OPEN;

function setConn(on) { const e = $('conn'); e.textContent = on ? 'connected' : 'offline'; e.className = 'pill ' + (on ? 'on' : 'off'); }
function setRec(on) { recording = on; $('rec').className = on ? 'pill rec' : 'pill hidden'; }

// ------------------------------------------------------------------ camera
async function startCamera() {
  stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: { facingMode: { ideal: 'environment' }, width: { ideal: cfg.width }, height: { ideal: cfg.height },
             frameRate: { ideal: cfg.fps, max: cfg.fps } },
  });
  video.srcObject = stream;
  await video.play();
  canvas = document.createElement('canvas');
  ctx = canvas.getContext('2d', { alpha: false });
  rvfc = 'requestVideoFrameCallback' in HTMLVideoElement.prototype;
  if (rvfc) video.requestVideoFrameCallback(onVideoFrame);
  else timers.push(setInterval(() => onFrame(performance.now(), 3), 1000 / cfg.fps / 2));
}

function onVideoFrame(now, md) {
  if (!running) return;
  // captureTime = when the camera captured the frame (same clock as performance.now).
  // Fall back to the callback time (later by the capture->page pipeline delay) when absent.
  let t = now, src = 2;
  if (md && typeof md.captureTime === 'number' && md.captureTime > 0 &&
      md.captureTime <= now + 1 && now - md.captureTime < 1000) { t = md.captureTime; src = 1; }
  onFrame(t, src);
  video.requestVideoFrameCallback(onVideoFrame);
}

function onFrame(tMs, src) {
  if (tMs - lastFrameT < 0.9 * (1000 / cfg.fps)) return; // throttle to the target rate
  const w = video.videoWidth, h = video.videoHeight;
  if (!w || !h) return; // camera not ready yet
  lastFrameT = tMs;
  counters.frames_captured++;
  const idx = frameIndex++; // every selected frame consumes an index -> gaps on the PC = drops
  if (!wsOpen() || ws.bufferedAmount > cfg.max_buffered_bytes) { counters.frames_dropped_backpressure++; return; }
  if (encoding) { counters.frames_dropped_busy++; return; }
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  ctx.drawImage(video, 0, 0, w, h);
  const orient = orientationDeg();
  encoding = true;
  canvas.toBlob((blob) => {
    encoding = false;
    if (!blob || !wsOpen()) { counters.frames_dropped_backpressure++; return; }
    const hdr = new ArrayBuffer(32), dv = new DataView(hdr);
    [0x56, 0x46, 0x52, 0x31].forEach((b, i) => dv.setUint8(i, b)); // "VFR1"
    dv.setUint32(4, idx, true);
    dv.setBigInt64(8, BigInt(Math.round(tMs * 1e6)), true);
    dv.setBigInt64(16, BigInt(nowNs()), true);
    dv.setUint16(24, w, true);
    dv.setUint16(26, h, true);
    dv.setUint8(28, src);
    dv.setUint8(29, (orient / 90) & 3);
    dv.setUint16(30, 0, true);
    ws.send(new Blob([hdr, blob]));
    counters.frames_sent++;
    tsSourceCount[src]++;
  }, 'image/jpeg', cfg.jpeg_quality);
}

// ------------------------------------------------------------------ IMU
function pushImu(type, tMs, x, y, z) {
  imuQueue.push([type, seq[type]++, Math.round(tMs * 1e6), x, y, z]);
  counters.imu_samples++;
}

function startImu() {
  // Preferred: Generic Sensor API (Chrome/Android): separate gyro + accel streams, per-reading timestamps.
  if ('Gyroscope' in window && 'Accelerometer' in window) {
    try {
      const g = new Gyroscope({ frequency: cfg.imu_hz, referenceFrame: 'device' });
      const a = new Accelerometer({ frequency: cfg.imu_hz, referenceFrame: 'device' });
      let failed = false;
      const onErr = (e) => {
        if (failed) return;
        failed = true;
        console.warn('Generic Sensor API failed, falling back to devicemotion:', e.error);
        g.stop(); a.stop(); sensors = [];
        startDeviceMotion();
      };
      g.onerror = onErr; a.onerror = onErr;
      g.onreading = () => pushImu('gyro', g.timestamp, g.x, g.y, g.z);              // rad/s
      a.onreading = () => pushImu('accel', a.timestamp, a.x, a.y, a.z);             // m/s^2 incl. gravity
      g.start(); a.start();
      sensors = [g, a];
      imuApi = 'generic_sensor';
      return;
    } catch (e) { console.warn(e); }
  }
  startDeviceMotion();
}

function onMotion(e) {
  const t = e.timeStamp; // same clock as performance.now
  const r = e.rotationRate, ag = e.accelerationIncludingGravity, D = Math.PI / 180;
  // W3C: alpha = about z, beta = about x, gamma = about y, in deg/s -> rad/s in body x,y,z order
  if (r && r.alpha !== null) pushImu('gyro', t, (r.beta || 0) * D, (r.gamma || 0) * D, (r.alpha || 0) * D);
  if (ag && ag.x !== null) pushImu('accel', t, ag.x, ag.y, ag.z);
}
function startDeviceMotion() {
  imuApi = 'devicemotion';
  window.addEventListener('devicemotion', onMotion);
  if (wsOpen()) ws.send(JSON.stringify(hello())); // tell the PC which API is active
}

function startGnss() {
  if (!cfg.gnss || !('geolocation' in navigator)) return;
  geoWatch = navigator.geolocation.watchPosition((p) => {
    const c = p.coords;
    gnssQueue.push({ seq: seq.gnss++, t: nowNs(), fix_ms: p.timestamp, lat: c.latitude, lon: c.longitude,
                     alt: c.altitude, acc: c.accuracy, alt_acc: c.altitudeAccuracy, speed: c.speed, heading: c.heading });
  }, (err) => console.warn('GNSS', err.message), { enableHighAccuracy: true, maximumAge: 0 });
}

function flushTelemetry() {
  if (!wsOpen()) {
    if (imuQueue.length > 2000) imuQueue.splice(0, imuQueue.length - 2000); // lost -> visible as seq gaps
    return;
  }
  if (imuQueue.length) { ws.send(JSON.stringify({ type: 'imu', session_id: SESSION_ID, samples: imuQueue })); imuQueue = []; }
  if (gnssQueue.length) { ws.send(JSON.stringify({ type: 'gnss', session_id: SESSION_ID, samples: gnssQueue })); gnssQueue = []; }
}

// ------------------------------------------------------------------ status
let prev = { t: performance.now(), sent: 0, imu: 0 };
function sendStatus() {
  const t = performance.now(), dt = (t - prev.t) / 1000;
  const fps = (counters.frames_sent - prev.sent) / dt, ihz = (counters.imu_samples - prev.imu) / dt;
  prev = { t, sent: counters.frames_sent, imu: counters.imu_samples };
  const s = stream && stream.getVideoTracks()[0] ? stream.getVideoTracks()[0].getSettings() : {};
  $('s-video').textContent = `${fps.toFixed(1)} fps sent, ${video.videoWidth}x${video.videoHeight} (cam ${s.frameRate ? Math.round(s.frameRate) : '?'} fps)`;
  $('s-imu').textContent = `${ihz.toFixed(0)} samples/s total via ${imuApi}`;
  $('s-drops').textContent = `busy ${counters.frames_dropped_busy}, backpressure ${counters.frames_dropped_backpressure}`;
  $('s-timing').textContent = `frame ts: ${tsSourceCount[1] ? 'captureTime' : (rvfc ? 'callback time' : 'poll time')}, ` +
    `isolated=${!!self.crossOriginIsolated}`;
  if (wsOpen()) {
    ws.send(JSON.stringify({ type: 'status', session_id: SESSION_ID, t_phone_ns: nowNs(), ...counters,
                             ws_buffered_bytes: ws.bufferedAmount, orientation_deg: orientationDeg(), imu_api: imuApi }));
  }
}

// ------------------------------------------------------------------ start / stop
async function start() {
  readSettings();
  $('start').disabled = true;
  try {
    // iOS requires an explicit permission request from a user gesture.
    if (typeof DeviceMotionEvent !== 'undefined' && typeof DeviceMotionEvent.requestPermission === 'function') {
      const p = await DeviceMotionEvent.requestPermission();
      if (p !== 'granted') throw new Error('motion permission denied');
    }
    await startCamera();
    running = true;
    startImu();
    startGnss();
    try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) { /* optional */ }
    connect();
    timers.push(setInterval(flushTelemetry, 40), setInterval(sendStatus, 1000));
    $('stop').disabled = false;
  } catch (e) {
    alert('Start failed: ' + e.message);
    stop();
  }
}

function stop() {
  running = false;
  timers.forEach(clearInterval); timers = [];
  sensors.forEach((s) => s.stop()); sensors = [];
  window.removeEventListener('devicemotion', onMotion);
  if (geoWatch !== null) navigator.geolocation.clearWatch(geoWatch);
  geoWatch = null;
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null;
  if (ws) { flushTelemetry(); ws.close(); }
  if (wakeLock) wakeLock.release().catch(() => {});
  $('start').disabled = false; $('stop').disabled = true;
}

document.addEventListener('visibilitychange', async () => {
  if (running && document.visibilityState === 'visible') {
    try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) { /* optional */ }
  }
});

$('start').onclick = start;
$('stop').onclick = stop;
$('quality').oninput = showQuality;
$('sid').textContent = 'session ' + SESSION_ID;
if (!window.isSecureContext) $('secure-warning').classList.remove('hidden');
loadConfig();
