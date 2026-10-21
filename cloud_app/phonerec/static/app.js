// Phone camera + IMU recorder. Uploads to the same origin with fetch() over HTTPS.
// Time basis: performance.now() (ms since the page's time origin) for every frame and IMU sample.
'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null); // keep null, never invent 0
  const vec = (o) => (o ? { x: num(o.x), y: num(o.y), z: num(o.z) } : null);
  const rot = (o) => (o ? { alpha: num(o.alpha), beta: num(o.beta), gamma: num(o.gamma) } : null);
  const mb = (n) => (n / 1e6).toFixed(n < 1e7 ? 2 : 1) + ' MB';
  const RETRYABLE = new Set([0, 408, 425, 429, 500, 502, 503, 504]);
  const FATAL = new Set([401, 403, 404, 409, 507]);
  const MAX_IMU_BUFFER = 6000;     // samples kept while uploads are failing (~30 s at 200 events/s)
  const MAX_IMU_BATCHES = 40;      // queued batches
  const MAX_EVENTS = 200;

  const S = {
    cfg: { width: 640, height: 480, fps: 5, jpeg_quality: 0.7, imu_batch_ms: 300, max_pending_frames: 10,
           upload_concurrency: 2, request_timeout_ms: 10000, max_retries: 3, stop_flush_timeout_ms: 15000,
           max_imu_batch: 2000, auth_required: true },
    target: null, token: '',
    stream: null, track: null, canvas: document.createElement('canvas'), ctx: null, rvfc: null,
    cameraReady: false, cameraError: '', motionOn: false, orientationOn: false, sensorsChecked: false,
    motionPerm: 'not-required', orientationPerm: 'not-required',
    motionSeen: false, orientationSeen: false, lastMotion: null, lastOrientation: null,
    sessionId: null, lastSessionId: null, recording: false, stopping: false, abandon: false, stopFailed: false,
    timers: [], encoding: false, frameSeq: 0, frameQueue: [], framesInflight: 0,
    imuBuffer: [], imuSeq: 0, batchSeq: 0, batchQueue: [], imuInflight: null,
    rate: { frames: [], motion: [], orientation: [] }, encodedW: null, encodedH: null,
    events: [], c: {}, lastError: 'none', wakeLock: null,
  };
  S.ctx = S.canvas.getContext('2d', { alpha: false });

  function resetCounters() {
    S.c = { frames_captured: 0, frames_dropped_busy: 0, frames_dropped_queue: 0, frames_dropped_not_ready: 0,
            frames_encode_failed: 0, frames_accepted: 0, frames_failed: 0, frames_unsent_at_stop: 0,
            imu_motion_events: 0, imu_orientation_events: 0, imu_samples_accepted: 0, imu_samples_dropped: 0,
            imu_samples_failed: 0, imu_samples_unsent_at_stop: 0, upload_retries: 0, page_hidden_count: 0 };
  }
  resetCounters();

  function setStatus(text, kind = 'info') { const e = $('status'); e.textContent = text; e.className = 'status ' + kind; }
  function logEvent(type, detail = '') {
    S.events.push({ t_mono_ms: performance.now(), type, detail: String(detail).slice(0, 300) });
    if (S.events.length > MAX_EVENTS) S.events.splice(0, S.events.length - MAX_EVENTS);
  }

  // ------------------------------------------------------------------ HTTP
  async function api(path, { method = 'GET', body, headers = {}, timeoutMs, raw = false } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs ?? S.cfg.request_timeout_ms);
    const h = { ...headers };
    if (S.token) h.Authorization = 'Bearer ' + S.token; // header only, never the URL
    try {
      const res = await fetch(path, { method, body, headers: h, signal: ctrl.signal, cache: 'no-store' });
      let data = null;
      if (!raw || !res.ok) { try { data = await res.json(); } catch (e) { data = null; } }
      return { ok: res.ok, status: res.status, data, res };
    } catch (e) {
      return { ok: false, status: 0, data: null,
               error: e.name === 'AbortError' ? 'request timed out' : 'network error (offline or server unreachable)' };
    } finally { clearTimeout(timer); }
  }

  function errText(r) {
    if (r.status === 0) return r.error || 'network error';
    const d = r.data && r.data.detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join('; ');
    return 'HTTP ' + r.status;
  }

  async function sendWithRetry(makeRequest) {
    let last = null;
    for (let attempt = 1; attempt <= S.cfg.max_retries + 1; attempt++) {
      if (S.abandon) return { ok: false, status: -1, error: 'abandoned at stop' };
      last = await makeRequest(attempt);
      if (last.ok || !RETRYABLE.has(last.status)) return last;
      if (attempt <= S.cfg.max_retries) {
        S.c.upload_retries++;
        await sleep(Math.min(4000, 500 * 2 ** (attempt - 1)) + Math.random() * 250);
      }
    }
    return last;
  }

  // ------------------------------------------------------------------ config + token
  async function loadConfig() {
    for (let i = 0; ; i++) {
      const r = await api('/api/config', { timeoutMs: 15000 });
      if (r.ok && r.data) { Object.assign(S.cfg, r.data); break; }
      setStatus(i === 0 ? 'Connecting to the server…' :
        'Server is waking up (free hosting can take up to a minute after being idle)…', 'warn');
      await sleep(3000);
    }
    $('access-card').hidden = !S.cfg.auth_required;
    const res = `${S.cfg.width}x${S.cfg.height}`;
    if (![...$('res').options].some((o) => o.value === res)) $('res').add(new Option(res.replace('x', ' × '), res));
    $('res').value = res;
    $('fps').value = String(S.cfg.fps);
    $('quality').value = String(S.cfg.jpeg_quality);
    readSettings();
    if (!window.isSecureContext) {
      setStatus('This page is not served over HTTPS, so the browser will block the camera and motion sensors. ' +
                'Open the https:// address of the deployed site.', 'error');
    } else {
      setStatus(S.cfg.auth_required ? 'Enter the access token, then tap “Enable camera & sensors”.'
                                    : 'Tap “Enable camera & sensors”.');
    }
  }

  function readSettings() {
    const [w, h] = $('res').value.split('x').map(Number);
    S.target = { width: w, height: h, fps: Number($('fps').value), quality: Number($('quality').value) };
    $('quality-out').textContent = S.target.quality.toFixed(2);
  }

  async function checkToken() {
    S.token = $('token').value.trim();
    if (!S.token) { setStatus('Enter the access token first.', 'error'); return false; }
    const r = await api('/api/auth/check');
    if (r.ok) { setStatus('Access token accepted.', 'ok'); return true; }
    setStatus(r.status === 401 ? 'Access token rejected. Check the value set in APP_ACCESS_TOKEN.' :
              'Token check failed: ' + errText(r), 'error');
    return false;
  }

  // ------------------------------------------------------------------ camera
  function cameraErrorMessage(e) {
    switch (e && e.name) {
      case 'NotAllowedError': return 'Camera permission denied. Allow the camera for this site in the browser ' +
        'settings (tap the lock or “aA” icon next to the address), then reload the page.';
      case 'NotFoundError': case 'OverconstrainedError': return 'No usable camera was found on this device.';
      case 'NotReadableError': case 'AbortError': return 'The camera is busy or could not start. Close other ' +
        'apps using the camera and try again.';
      case 'SecurityError': return 'The browser blocked the camera (the page must be opened over HTTPS).';
      default: return 'Camera error: ' + (e ? `${e.name}: ${e.message}` : 'unknown');
    }
  }

  async function startCamera() {
    if (!window.isSecureContext) throw new Error('Camera needs HTTPS: open the https:// address of the site.');
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error('This browser cannot access the camera (getUserMedia is not available).');
    }
    const size = { width: { ideal: S.target.width }, height: { ideal: S.target.height } };
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia(
        { audio: false, video: { ...size, facingMode: { ideal: 'environment' } } });
    } catch (e) {
      if (e.name === 'NotAllowedError' || e.name === 'SecurityError') throw new Error(cameraErrorMessage(e));
      logEvent('camera_fallback', e.name);
      try { stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: true }); } catch (e2) {
        throw new Error(cameraErrorMessage(e2));
      }
    }
    S.stream = stream;
    S.track = stream.getVideoTracks()[0];
    S.track.addEventListener('ended', () => {
      logEvent('camera_ended');
      if (S.recording) setStatus('The camera stopped (the browser or another app took it). Frames are no longer ' +
                                 'captured; stop this recording and start a new one.', 'warn');
    });
    const v = $('preview');
    v.muted = true; v.playsInline = true; v.srcObject = stream;
    try { await v.play(); } catch (e) { /* autoplay with muted+playsinline normally works */ }
    for (let i = 0; i < 30 && !v.videoWidth; i++) await sleep(100); // wait for dimensions
    if ('requestVideoFrameCallback' in HTMLVideoElement.prototype) {
      const loop = (now, md) => {
        S.rvfc = { captureTime: num(md && md.captureTime), mediaTime: num(md && md.mediaTime) };
        if (S.stream) v.requestVideoFrameCallback(loop);
      };
      v.requestVideoFrameCallback(loop);
    }
    S.cameraReady = true;
  }

  function stopCamera() {
    if (S.stream) S.stream.getTracks().forEach((t) => t.stop());
    S.stream = null; S.track = null; S.cameraReady = false; S.rvfc = null;
    $('preview').srcObject = null;
  }

  function trackSettings() {
    const out = {};
    if (!S.track || !S.track.getSettings) return out;
    for (const [k, v] of Object.entries(S.track.getSettings())) {
      if (k === 'deviceId' || k === 'groupId') continue; // identifiers are not needed
      if (v === null || ['string', 'number', 'boolean'].includes(typeof v)) out[k] = v;
    }
    return out;
  }

  // ------------------------------------------------------------------ sensors
  function onMotion(e) {
    const t = performance.now();
    S.motionSeen = true;
    S.lastMotion = { acc: vec(e.acceleration), accG: vec(e.accelerationIncludingGravity),
                     rot: rot(e.rotationRate), interval: num(e.interval) };
    S.rate.motion.push(t);
    if (!S.recording) return;
    S.c.imu_motion_events++;
    pushImu({ seq: S.imuSeq++, kind: 'motion', t_mono_ms: t, event_ts_ms: num(e.timeStamp),
              interval: S.lastMotion.interval, acceleration: S.lastMotion.acc,
              accelerationIncludingGravity: S.lastMotion.accG, rotationRate: S.lastMotion.rot });
  }

  function onOrientation(e) {
    const t = performance.now();
    S.orientationSeen = true;
    S.lastOrientation = { alpha: num(e.alpha), beta: num(e.beta), gamma: num(e.gamma),
                          absolute: typeof e.absolute === 'boolean' ? e.absolute : null };
    S.rate.orientation.push(t);
    if (!S.recording) return;
    S.c.imu_orientation_events++;
    pushImu({ seq: S.imuSeq++, kind: 'orientation', t_mono_ms: t, event_ts_ms: num(e.timeStamp),
              orientation: S.lastOrientation });
  }

  function startSensors() {
    if ('DeviceMotionEvent' in window && S.motionPerm !== 'denied' && !S.motionOn) {
      window.addEventListener('devicemotion', onMotion); S.motionOn = true;
    }
    if ('DeviceOrientationEvent' in window && S.orientationPerm !== 'denied' && !S.orientationOn) {
      window.addEventListener('deviceorientation', onOrientation); S.orientationOn = true;
    }
  }

  function stopSensors() {
    window.removeEventListener('devicemotion', onMotion);
    window.removeEventListener('deviceorientation', onOrientation);
    S.motionOn = false; S.orientationOn = false;
  }

  function sensorNote() {
    const parts = [];
    if (!('DeviceMotionEvent' in window)) parts.push('This browser has no motion sensor API (DeviceMotionEvent).');
    else if (S.motionPerm === 'denied') parts.push('Motion access was denied. On iPhone, reload the page and ' +
      'tap “Allow” when asked for Motion & Orientation access.');
    else if (!S.motionSeen) parts.push('No motion events received: this device or browser does not provide ' +
      'motion sensors (common on laptops and desktops).');
    else {
      const m = S.lastMotion, miss = [];
      if (!m.acc || m.acc.x === null) miss.push('acceleration without gravity');
      if (!m.accG || m.accG.x === null) miss.push('acceleration with gravity');
      if (!m.rot || m.rot.alpha === null) miss.push('rotation rate (gyroscope)');
      parts.push(miss.length ? 'Unavailable on this device: ' + miss.join(', ') + '.' : 'Motion sensors OK.');
    }
    const oriOk = S.orientationSeen && S.lastOrientation && S.lastOrientation.alpha !== null;
    if (!oriOk) parts.push('Orientation: unavailable.');
    const lm = S.lastMotion;
    const motionOk = lm && ((lm.acc && lm.acc.x !== null) || (lm.accG && lm.accG.x !== null) ||
                            (lm.rot && lm.rot.alpha !== null));
    if (!motionOk && !oriOk) parts.push('Desktop browsers usually report empty sensor events. Camera-only recording still works.');
    return parts.join(' ');
  }

  // ------------------------------------------------------------------ enable (user gesture)
  async function enable() {
    readSettings();
    $('enable').disabled = true;
    // iOS: permission requests must start directly inside the tap handler, before any other await.
    const ask = (C) => (typeof C !== 'undefined' && typeof C.requestPermission === 'function')
      ? C.requestPermission() : Promise.resolve('not-required');
    const [pm, po] = await Promise.allSettled([ask(window.DeviceMotionEvent), ask(window.DeviceOrientationEvent)]);
    S.motionPerm = pm.status === 'fulfilled' ? pm.value : 'error';
    S.orientationPerm = po.status === 'fulfilled' ? po.value : 'error';
    startSensors();

    setStatus('Starting the camera…');
    try {
      await startCamera();
      S.cameraError = '';
    } catch (e) {
      S.cameraError = e.message;
      logEvent('camera_error', e.message);
    }
    await sleep(1500); // give sensors time to deliver a first event
    S.sensorsChecked = true;
    $('sensor-note').textContent = sensorNote();
    const anySensor = S.motionSeen || S.orientationSeen;
    if (!S.cameraReady && !anySensor) {
      setStatus((S.cameraError || 'Camera unavailable.') + ' No motion sensors either, so nothing can be recorded.', 'error');
      $('enable').disabled = false;
      return;
    }
    $('start').disabled = false;
    if (!S.cameraReady) setStatus(S.cameraError + ' You can still record sensors only.', 'warn');
    else if (!anySensor) setStatus('Camera ready. Motion sensors unavailable: recording will be camera-only.', 'warn');
    else setStatus('Camera and sensors ready. Tap “Start recording”.', 'ok');
  }

  // ------------------------------------------------------------------ recording
  function fitSize(vw, vh, tw, th) {
    const s = Math.min(1, Math.max(tw, th) / Math.max(vw, vh), Math.min(tw, th) / Math.min(vw, vh));
    return [Math.max(2, Math.round(vw * s / 2) * 2), Math.max(2, Math.round(vh * s / 2) * 2)];
  }

  async function startRecording() {
    if (S.cfg.auth_required) {
      S.token = $('token').value.trim();
      if (!S.token) { setStatus('Enter the access token first (section 1).', 'error'); $('token').focus(); return; }
    }
    readSettings();
    if (!S.cameraReady && !S.motionOn && !S.orientationOn) {
      setStatus('Tap “Enable camera & sensors” first.', 'error'); return;
    }
    $('start').disabled = true;
    setStatus('Creating a recording session…');
    resetCounters();
    Object.assign(S, { events: [], frameSeq: 0, imuSeq: 0, batchSeq: 0, imuBuffer: [], batchQueue: [],
                       frameQueue: [], abandon: false, stopFailed: false, lastError: 'none',
                       encodedW: null, encodedH: null });
    S.rate.frames = [];
    const v = $('preview');
    const m = S.lastMotion;
    const body = {
      client: { user_agent: navigator.userAgent.slice(0, 512), platform: (navigator.platform || '').slice(0, 64),
                language: (navigator.language || '').slice(0, 32), screen_width: screen.width,
                screen_height: screen.height, device_pixel_ratio: num(window.devicePixelRatio),
                time_origin_unix_ms: num(performance.timeOrigin), secure_context: window.isSecureContext },
      requested: { width: S.target.width, height: S.target.height, fps: S.target.fps,
                   jpeg_quality: S.target.quality, imu_batch_ms: S.cfg.imu_batch_ms },
      camera: { available: S.cameraReady, label: S.track ? (S.track.label || '').slice(0, 256) : '',
                facing_mode_requested: 'environment', track_settings: trackSettings(),
                video_width: v.videoWidth || null, video_height: v.videoHeight || null,
                error: (S.cameraError || '').slice(0, 300) },
      sensors: { device_motion_api: 'DeviceMotionEvent' in window,
                 device_orientation_api: 'DeviceOrientationEvent' in window,
                 motion_permission: S.motionPerm, orientation_permission: S.orientationPerm,
                 motion_events_seen: S.motionSeen, orientation_events_seen: S.orientationSeen,
                 acceleration_available: m ? !!(m.acc && m.acc.x !== null) : null,
                 acceleration_including_gravity_available: m ? !!(m.accG && m.accG.x !== null) : null,
                 rotation_rate_available: m ? !!(m.rot && m.rot.alpha !== null) : null,
                 orientation_available: S.orientationSeen ? !!(S.lastOrientation && S.lastOrientation.alpha !== null) : null,
                 note: sensorNote().slice(0, 500) },
    };
    const r = await api('/api/sessions', { method: 'POST', body: JSON.stringify(body),
                                            headers: { 'Content-Type': 'application/json' } });
    if (!r.ok) {
      setStatus('Could not start recording: ' + errText(r), 'error');
      $('start').disabled = false;
      return;
    }
    S.sessionId = r.data.session_id; S.lastSessionId = S.sessionId;
    S.recording = true; S.stopping = false;
    logEvent('recording_started');
    if (S.cameraReady) S.timers.push(setInterval(captureFrame, 1000 / S.target.fps));
    S.timers.push(setInterval(() => flushImu(false), S.cfg.imu_batch_ms));
    try { S.wakeLock = await navigator.wakeLock.request('screen'); } catch (e) { /* optional */ }
    $('stop').disabled = false; $('download').disabled = true; $('settings').open = false;
    setStatus('Recording. Keep this page open and the screen on.', 'ok');
  }

  function captureFrame() {
    if (!S.recording) return;
    const v = $('preview');
    if (!S.track || S.track.readyState !== 'live' || v.readyState < 2 || !v.videoWidth) {
      S.c.frames_dropped_not_ready++; return;
    }
    if (S.encoding) { S.c.frames_dropped_busy++; return; }                 // never overlap encodes
    if (S.frameQueue.length + S.framesInflight >= S.cfg.max_pending_frames) {
      S.c.frames_dropped_queue++; return;                                   // bounded upload work
    }
    const [w, h] = fitSize(v.videoWidth, v.videoHeight, S.target.width, S.target.height);
    if (S.canvas.width !== w || S.canvas.height !== h) { S.canvas.width = w; S.canvas.height = h; }
    const seq = S.frameSeq++, sid = S.sessionId, rv = S.rvfc;
    const tCap = performance.now();                                        // capture-time estimate
    S.ctx.drawImage(v, 0, 0, w, h);
    S.encoding = true;
    S.canvas.toBlob((blob) => {
      S.encoding = false;
      if (!blob) { S.c.frames_encode_failed++; return; }
      S.c.frames_captured++; S.rate.frames.push(tCap); S.encodedW = w; S.encodedH = h;
      S.frameQueue.push({ sid, seq, blob, tCap, tEnc: performance.now(), w, h,
                          rvfcCapture: rv ? rv.captureTime : null, rvfcMedia: rv ? rv.mediaTime : null });
      pumpFrames();
    }, 'image/jpeg', S.target.quality);
  }

  function pumpFrames() {
    if (S.abandon) { S.c.frames_unsent_at_stop += S.frameQueue.length; S.frameQueue = []; return; }
    while (S.framesInflight < S.cfg.upload_concurrency && S.frameQueue.length) {
      const item = S.frameQueue.shift();
      S.framesInflight++;
      uploadFrame(item).finally(() => { S.framesInflight--; pumpFrames(); });
    }
  }

  async function uploadFrame(it) {
    const r = await sendWithRetry((attempt) => {
      const q = new URLSearchParams({ seq: it.seq, t_capture_mono_ms: it.tCap.toFixed(3),
                                      t_encoded_mono_ms: it.tEnc.toFixed(3), width: it.w, height: it.h, attempt });
      if (it.rvfcCapture !== null) q.set('rvfc_capture_time_ms', it.rvfcCapture.toFixed(3));
      if (it.rvfcMedia !== null) q.set('rvfc_media_time_s', it.rvfcMedia.toFixed(6));
      return api(`/api/sessions/${it.sid}/frames?${q}`, { method: 'POST', body: it.blob,
                                                          headers: { 'Content-Type': 'image/jpeg' } });
    });
    if (r.ok) S.c.frames_accepted++;               // "duplicate" also means it is stored
    else if (r.status === -1) S.c.frames_unsent_at_stop++;
    else { S.c.frames_failed++; uploadFailed('Frame', r); }
  }

  function pushImu(sample) {
    S.imuBuffer.push(sample);
    if (S.imuBuffer.length > MAX_IMU_BUFFER) { S.imuBuffer.shift(); S.c.imu_samples_dropped++; }
  }

  function flushImu(all) {
    while (S.imuBuffer.length) {
      const samples = S.imuBuffer.splice(0, S.cfg.max_imu_batch);
      S.batchQueue.push({ sid: S.sessionId, batch_seq: S.batchSeq++, samples });
      if (!all) break;
    }
    while (S.batchQueue.length > MAX_IMU_BATCHES) {         // bounded: drop the oldest, and count it
      S.c.imu_samples_dropped += S.batchQueue.shift().samples.length;
    }
    pumpImu();
  }

  function pumpImu() {
    if (S.abandon) {
      S.c.imu_samples_unsent_at_stop += S.imuBuffer.length + S.batchQueue.reduce((n, b) => n + b.samples.length, 0);
      S.imuBuffer = []; S.batchQueue = []; return;
    }
    if (S.imuInflight || !S.batchQueue.length) return;
    const b = S.batchQueue.shift();
    S.imuInflight = b;
    uploadImu(b).finally(() => { S.imuInflight = null; pumpImu(); });
  }

  async function uploadImu(b) {
    const body = JSON.stringify({ batch_seq: b.batch_seq, samples: b.samples });
    const r = await sendWithRetry(() => api(`/api/sessions/${b.sid}/imu`, {
      method: 'POST', body, headers: { 'Content-Type': 'application/json' } }));
    if (r.ok) S.c.imu_samples_accepted += b.samples.length; // a duplicate ack means an earlier try was stored
    else if (r.status === -1) S.c.imu_samples_unsent_at_stop += b.samples.length;
    else { S.c.imu_samples_failed += b.samples.length; uploadFailed('IMU', r); }
  }

  function uploadFailed(kind, r) {
    S.lastError = `${kind} upload failed (${r.status || 'network'}): ${errText(r)}`;
    logEvent('upload_failed', S.lastError);
    if (!FATAL.has(r.status) || !S.recording) return;
    const msg = {
      401: 'The access token was rejected, so recording stopped. Enter the correct token and start again.',
      403: 'The server refused the upload, so recording stopped.',
      404: 'The server no longer has this recording (it restarted, the recording expired, or it was deleted). ' +
           'Data uploaded so far is lost on the server. Start a new recording.',
      409: 'The server ended this recording (time limit or inactivity). What was uploaded can be downloaded.',
      507: 'Server storage is full, so recording stopped. Download and delete old recordings.',
    }[r.status];
    haltCapture();
    S.abandon = true; pumpFrames(); pumpImu();
    setStatus(msg + ' (' + errText(r) + ')', 'error');
    if (r.status === 409 || r.status === 507) finishStop('server_' + r.status);
    else { S.sessionId = null; $('stop').disabled = true; $('start').disabled = false; }
  }

  function haltCapture() {
    S.recording = false;
    S.timers.forEach(clearInterval); S.timers = [];
    if (S.wakeLock) { S.wakeLock.release().catch(() => {}); S.wakeLock = null; }
  }

  const pendingFrames = () => S.frameQueue.length + S.framesInflight + (S.encoding ? 1 : 0);
  const pendingImu = () => S.imuBuffer.length + S.batchQueue.reduce((n, b) => n + b.samples.length, 0) +
                           (S.imuInflight ? S.imuInflight.samples.length : 0);

  async function stopRecording() {
    if (!S.sessionId) return;
    if (S.stopFailed) { finishStop('user_retry'); return; }   // capture already stopped; resend only
    if (S.stopping) return;
    S.stopping = true;
    $('stop').disabled = true;
    haltCapture();
    stopSensors();
    logEvent('recording_stopped');
    flushImu(true);
    setStatus('Stopping: uploading the remaining data…');
    const deadline = performance.now() + S.cfg.stop_flush_timeout_ms;
    while (performance.now() < deadline && pendingFrames() + pendingImu() > 0) { await sleep(200); render(); }
    if (pendingFrames() + pendingImu() > 0) {
      S.abandon = true; pumpFrames(); pumpImu();              // count queued data as not uploaded
      const until = performance.now() + S.cfg.request_timeout_ms + 1000;
      while (performance.now() < until && (S.framesInflight || S.imuInflight || S.encoding)) await sleep(200);
    }
    const pv = $('preview');
    S.lastVideoW = pv.videoWidth || null; S.lastVideoH = pv.videoHeight || null;  // before releasing the camera
    stopCamera();
    await finishStop('user');
  }

  async function finishStop(reason) {
    const sid = S.sessionId;
    if (!sid) return;
    const v = $('preview');
    const body = { reason, counters: S.c, events: S.events,
                   observed: { frame_fps: hz(S.rate.frames, 3000), motion_hz: hz(S.rate.motion, 3000),
                               orientation_hz: hz(S.rate.orientation, 3000),
                               video_width: v.videoWidth || S.lastVideoW || null,
                               video_height: v.videoHeight || S.lastVideoH || null,
                               encoded_width: S.encodedW, encoded_height: S.encodedH } };
    let r = null;
    for (let i = 0; i < 3; i++) {
      r = await api(`/api/sessions/${sid}/stop`, { method: 'POST', body: JSON.stringify(body),
                                                   headers: { 'Content-Type': 'application/json' } });
      if (r.ok || !RETRYABLE.has(r.status)) break;
      await sleep(1000 * (i + 1));
    }
    S.stopping = false;
    if (!r.ok) {
      S.stopFailed = true;
      $('stop').disabled = false;
      setStatus('Could not finalize the recording: ' + errText(r) + '. Tap “Stop recording” to retry. ' +
                'If the server stays unreachable it finalizes the recording itself after 10 minutes.', 'error');
      return;
    }
    S.stopFailed = false; S.sessionId = null;
    $('download').disabled = false; $('start').disabled = !(S.cameraReady || S.motionOn || S.orientationOn);
    $('enable').disabled = false;
    const sc = r.data.server_counters || {};
    const lost = S.c.frames_failed + S.c.frames_unsent_at_stop;
    const lostImu = S.c.imu_samples_failed + S.c.imu_samples_unsent_at_stop + S.c.imu_samples_dropped;
    const dropped = S.c.frames_dropped_busy + S.c.frames_dropped_queue;
    const text = `Saved on the server: ${sc.frames_accepted ?? '?'} frames and ` +
      `${(sc.imu_motion_samples ?? 0) + (sc.imu_orientation_samples ?? 0)} IMU samples. ` +
      (lost || lostImu ? `Not uploaded: ${lost} frames, ${lostImu} IMU samples. ` : 'Nothing was lost in upload. ') +
      (dropped ? `${dropped} frames were skipped on the phone to keep up. ` : '') +
      'Download it now: the server keeps recordings only temporarily.';
    setStatus(text, lost || lostImu ? 'warn' : 'ok');
    listSessions();
  }

  // ------------------------------------------------------------------ download / list
  async function downloadSession(id) {
    if (S.cfg.auth_required && !S.token) S.token = $('token').value.trim();
    setStatus('Preparing the download…');
    const r = await api(`/api/sessions/${id}/download`, { raw: true, timeoutMs: 120000 });
    if (!r.ok) { setStatus('Download failed: ' + errText(r), 'error'); return; }
    let blob;
    const total = Number(r.res.headers.get('content-length')) || 0;
    try {
      if (r.res.body && r.res.body.getReader) {
        const reader = r.res.body.getReader(), chunks = [];
        let got = 0;
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value); got += value.length;
          setStatus(`Downloading… ${mb(got)}${total ? ' of ' + mb(total) : ''}`);
        }
        blob = new Blob(chunks, { type: 'application/zip' });
      } else {
        blob = await r.res.blob();
      }
    } catch (e) {
      setStatus('Download interrupted: ' + e.message + '. Try again.', 'error'); return;
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `recording-${id}.zip`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    setStatus(`Downloaded recording-${id}.zip (${mb(blob.size)}).`, 'ok');
  }

  async function deleteSession(id) {
    if (!confirm(`Delete recording ${id} from the server? This cannot be undone.`)) return;
    const r = await api(`/api/sessions/${id}`, { method: 'DELETE' });
    setStatus(r.ok ? `Deleted ${id}.` : 'Delete failed: ' + errText(r), r.ok ? 'ok' : 'error');
    listSessions();
  }

  async function listSessions() {
    if (S.cfg.auth_required) {
      S.token = $('token').value.trim();
      if (!S.token) { setStatus('Enter the access token to list recordings.', 'error'); return; }
    }
    const r = await api('/api/sessions');
    const box = $('sessions');
    box.textContent = '';
    if (!r.ok) { setStatus('Could not list recordings: ' + errText(r), 'error'); return; }
    $('storage').textContent = `Server storage used: ${mb(r.data.total_bytes)} of ${mb(r.data.max_total_bytes)}.`;
    if (!r.data.sessions.length) { box.textContent = 'No recordings on the server.'; return; }
    const table = document.createElement('table');
    const head = table.insertRow();
    ['Started (UTC)', 'Status', 'Length', 'Frames', 'IMU', 'Size', ''].forEach((t) => {
      const th = document.createElement('th'); th.textContent = t; head.appendChild(th);
    });
    for (const s of r.data.sessions) {
      const tr = table.insertRow();
      [s.created_utc.replace('T', ' ').slice(0, 19), s.status, s.duration_s + ' s', s.frames, s.imu_samples,
       mb(s.bytes)].forEach((t) => { tr.insertCell().textContent = t; });
      const cell = tr.insertCell();
      const dl = document.createElement('button');
      dl.type = 'button'; dl.textContent = 'Download'; dl.disabled = !s.downloadable;
      dl.title = s.downloadable ? s.session_id : 'Still recording';
      dl.addEventListener('click', () => downloadSession(s.session_id));
      const del = document.createElement('button');
      del.type = 'button'; del.className = 'secondary'; del.textContent = 'Delete'; del.disabled = !s.downloadable;
      del.addEventListener('click', () => deleteSession(s.session_id));
      cell.append(dl, ' ', del);
    }
    box.appendChild(table);
  }

  // ------------------------------------------------------------------ UI refresh
  function hz(list, windowMs) {
    const now = performance.now();
    while (list.length && now - list[0] > windowMs) list.shift();
    return list.length > 1 ? (list.length - 1) / ((list[list.length - 1] - list[0]) / 1000) : null;
  }
  const fmtHz = (v) => (v === null ? 'none' : v.toFixed(1) + ' /s');
  function cell(id, v, digits) {
    const e = $(id);
    if (v === undefined) { e.textContent = '–'; e.className = ''; return; }        // no event yet
    e.textContent = v === null ? 'unavailable' : v.toFixed(digits);
    e.className = v === null ? 'na' : '';
  }

  function render() {
    const c = S.c, v = $('preview');
    $('v-session').textContent = S.sessionId || (S.lastSessionId ? S.lastSessionId + ' (stopped)' : 'none');
    $('v-camera').textContent = S.cameraReady ? `${v.videoWidth} × ${v.videoHeight}` +
      (S.track && S.track.readyState !== 'live' ? ' (stopped)' : '') : (S.cameraError ? 'unavailable' : 'not enabled');
    $('v-encoded').textContent = S.encodedW ? `${S.encodedW} × ${S.encodedH}` : '–';
    $('v-fps').textContent = fmtHz(hz(S.rate.frames, 3000)) + (S.recording ? ` (target ${S.target.fps})` : '');
    $('v-frames').textContent = `${c.frames_captured} / ${c.frames_accepted}`;
    $('v-frames-bad').textContent = `${c.frames_failed + c.frames_unsent_at_stop} / ` +
      `${c.frames_dropped_busy + c.frames_dropped_queue + c.frames_dropped_not_ready + c.frames_encode_failed}`;
    $('v-fqueue').textContent = String(pendingFrames());
    $('v-motion-hz').textContent = S.motionOn || S.motionSeen ? fmtHz(hz(S.rate.motion, 2000)) : 'off';
    $('v-orient-hz').textContent = S.orientationOn || S.orientationSeen ? fmtHz(hz(S.rate.orientation, 2000)) : 'off';
    $('v-imu').textContent = `${c.imu_motion_events + c.imu_orientation_events} / ${c.imu_samples_accepted}`;
    $('v-imu-bad').textContent = `${c.imu_samples_failed + c.imu_samples_unsent_at_stop} / ${c.imu_samples_dropped}`;
    $('v-iqueue').textContent = String(pendingImu());
    $('v-retries').textContent = String(c.upload_retries);
    $('v-error').textContent = S.lastError;
    const m = S.lastMotion, o = S.lastOrientation;
    const none = S.sensorsChecked ? null : undefined;   // checked and nothing arrived -> unavailable
    const g = (obj, k) => (m ? (obj ? obj[k] : null) : none);
    ['x', 'y', 'z'].forEach((k, i) => { cell('s-acc-' + i, g(m && m.acc, k), 2); cell('s-accg-' + i, g(m && m.accG, k), 2); });
    ['alpha', 'beta', 'gamma'].forEach((k, i) => {
      cell('s-rot-' + i, g(m && m.rot, k), 1);
      cell('s-ori-' + i, o ? o[k] : none, 1);
    });
  }

  // ------------------------------------------------------------------ lifecycle
  document.addEventListener('visibilitychange', async () => {
    if (!S.recording) return;
    if (document.visibilityState === 'hidden') {
      S.c.page_hidden_count++; logEvent('page_hidden');
    } else {
      logEvent('page_visible');
      try { S.wakeLock = await navigator.wakeLock.request('screen'); } catch (e) { /* optional */ }
      if (!S.track || S.track.readyState !== 'live') {
        setStatus('While the page was hidden the browser stopped the camera. Stop this recording and start a new one.', 'warn');
      } else {
        setStatus('Recording resumed. Data from while the page was hidden may be missing.', 'warn');
      }
    }
  });
  window.addEventListener('offline', () => {
    logEvent('offline');
    if (S.recording) setStatus('The phone is offline. Data is buffered for a short time and retried; ' +
                               'frames are dropped once the queue is full.', 'warn');
  });
  window.addEventListener('online', () => { logEvent('online'); if (S.recording) setStatus('Back online. Recording.', 'ok'); });
  window.addEventListener('pagehide', () => { haltCapture(); stopSensors(); stopCamera(); });

  $('enable').addEventListener('click', enable);
  $('start').addEventListener('click', startRecording);
  $('stop').addEventListener('click', stopRecording);
  $('download').addEventListener('click', () => S.lastSessionId && downloadSession(S.lastSessionId));
  $('refresh').addEventListener('click', listSessions);
  $('check-token').addEventListener('click', checkToken);
  $('token').addEventListener('input', () => { S.token = $('token').value.trim(); });
  $('quality').addEventListener('input', readSettings);
  $('res').addEventListener('change', readSettings);
  $('fps').addEventListener('change', readSettings);
  setInterval(render, 300);
  loadConfig();
})();
