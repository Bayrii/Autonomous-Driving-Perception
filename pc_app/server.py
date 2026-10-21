"""PC ingest server: serves the phone web client and receives its WebSocket stream.

Runs its own asyncio loop on a background thread so the OpenCV viewer can own
the main thread. Everything received is (1) pushed into LiveState for display
and (2) handed to the Recorder, which writes on its own thread.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import ssl
import threading
from pathlib import Path

from aiohttp import WSMsgType, web

from .certs import ensure_cert, lan_ips
from .clock import SyncSample, pc_now_ns
from .config import ROOT, resolve
from .live_state import LiveState
from .protocol import PROTOCOL_VERSION, parse_imu_samples, unpack_frame
from .recorder import Recorder

log = logging.getLogger("pc_app.server")
CLIENT_DIR = ROOT / "phone_client"


@web.middleware
async def _headers(request, handler):
    resp = await handler(request)
    if not resp.prepared:
        # Cross-origin isolation gives the phone page a finer performance.now() (5 us instead of 100 us).
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        resp.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
        resp.headers["Cache-Control"] = "no-store"
    return resp


class PhoneServer:
    def __init__(self, cfg: dict, state: LiveState | None = None, recorder: Recorder | None = None,
                 enable_https: bool = True):
        self.cfg = cfg
        self.state = state or LiveState()
        self.recorder = recorder or Recorder(resolve(cfg["recording"]["sessions_dir"]))
        self.enable_https = enable_https
        self.want_record = False
        self.urls: list[str] = []
        self.phone_url: str | None = None  # the address a phone should open (shown as a QR code)
        self.access_key: str | None = None  # required on /ws/phone once the server is publicly reachable
        self.client_overrides: dict = {}    # stream defaults sent to the page (e.g. lighter for mobile data)
        self.http_port: int | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ws: web.WebSocketResponse | None = None
        self._rec_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._runner: web.AppRunner | None = None
        self.last_recording: Path | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        ready, err = threading.Event(), []

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self._start_sites())
            except Exception as e:  # e.g. port in use
                err.append(e)
                ready.set()
                return
            ready.set()
            self.loop.run_forever()
            self.loop.run_until_complete(self._runner.cleanup())
            self.loop.close()

        self._thread = threading.Thread(target=run, name="phone-server", daemon=True)
        self._thread.start()
        ready.wait()
        if err:
            raise err[0]

    def stop(self) -> Path | None:
        self.set_recording(False)
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=5)
        return self.last_recording

    async def _start_sites(self) -> None:
        s = self.cfg["server"]
        app = web.Application(middlewares=[_headers])
        app.router.add_get("/", self._index)
        app.router.add_get("/config.json", self._client_config)
        app.router.add_get("/ws/phone", self._ws_phone)
        app.router.add_static("/static/", CLIENT_DIR)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()

        http = web.TCPSite(self._runner, s["http_host"], s["http_port"])
        await http.start()
        self.http_port = self._runner.addresses[0][1]  # actual port (config may say 0 in tests)
        self.urls.append(f"http://localhost:{self.http_port}/   (this PC, or phone over USB with `adb reverse`)")
        self.phone_url = f"http://localhost:{self.http_port}/"

        if self.enable_https:
            cert, key = ensure_cert(resolve(s["cert_dir"]))
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(cert, key)
            await web.TCPSite(self._runner, s["https_host"], s["https_port"], ssl_context=ctx).start()
            for i, ip in enumerate(ip for ip in lan_ips() if ip != "127.0.0.1"):
                note = "phone on the same Wi-Fi" if i == 0 else "other adapter (VPN/VM?)"
                self.urls.append(f"https://{ip}:{s['https_port']}/   ({note})")
                if i == 0:
                    self.phone_url = f"https://{ip}:{s['https_port']}/"

    # ------------------------------------------------------------------ http
    async def _index(self, request):
        return web.FileResponse(CLIENT_DIR / "index.html")

    async def _client_config(self, request):
        return web.json_response({**self.cfg["client"], **self.client_overrides, "protocol_version": PROTOCOL_VERSION})

    def set_public_url(self, base_url: str, client_overrides: dict | None = None) -> str:
        """Expose the server at a public HTTPS address (tunnel). Phones then need the access key."""
        if self.access_key is None:
            # persisted, so the phone link stays the same across restarts (bookmark it on the phone)
            key_file = resolve(self.cfg["server"]["cert_dir"]) / "access_key.txt"
            if key_file.exists() and key_file.read_text().strip():
                self.access_key = key_file.read_text().strip()
            else:
                key_file.parent.mkdir(parents=True, exist_ok=True)
                self.access_key = secrets.token_urlsafe(9)
                key_file.write_text(self.access_key)
        self.client_overrides = dict(client_overrides or {})
        self.phone_url = f"{base_url.rstrip('/')}/?k={self.access_key}"
        self.urls.insert(0, f"{self.phone_url}   (public link: phone on ANY network, e.g. mobile data)")
        return self.phone_url

    # ------------------------------------------------------------------ recording control (any thread)
    def set_recording(self, on: bool) -> None:
        with self._rec_lock:
            self.want_record = on
            if on and not self.recorder.active and self.state.session_id:
                d = self.recorder.start(self.state.session_id, self.state.hello, self.cfg)
                log.info("recording -> %s", d)
            elif not on and self.recorder.active:
                self.last_recording = self.recorder.stop()
                log.info("recording saved: %s", self.last_recording)
        self._notify_phone()

    def _on_session(self, sid: str, hello: dict) -> None:
        """Runs in an executor thread when a phone says hello."""
        with self._rec_lock:
            if self.recorder.active and self.recorder.session_id != sid:
                # New page load = new phone clock domain -> must be a new recording.
                self.last_recording = self.recorder.stop()
                log.info("phone session changed; recording saved: %s", self.last_recording)
            if self.want_record and not self.recorder.active:
                log.info("recording -> %s", self.recorder.start(sid, hello, self.cfg))
            elif self.recorder.active:
                self.recorder.update_hello(hello)
                self.recorder.event("reconnect", sid)

    def _notify_phone(self) -> None:
        ws, loop = self._ws, self.loop
        if ws is None or loop is None or ws.closed:
            return
        msg = {"type": "state", "recording": self.recorder.active,
               "rec_id": self.recorder.dir.name if self.recorder.active else None}
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)

    # ------------------------------------------------------------------ websocket
    async def _ping_loop(self, ws: web.WebSocketResponse, pending: dict[int, int]) -> None:
        i = 0
        period = float(self.cfg["server"]["ping_interval_s"])
        while not ws.closed:
            i += 1
            data = json.dumps({"type": "ping", "id": i})
            pending[i] = pc_now_ns()
            await ws.send_str(data)
            for k in [k for k, t in pending.items() if pc_now_ns() - t > 10e9]:
                del pending[k]
            await asyncio.sleep(period)

    async def _ws_phone(self, request):
        if self.access_key is not None and not secrets.compare_digest(request.query.get("k", ""), self.access_key):
            log.warning("rejected phone connection from %s: missing or wrong access key", request.remote)
            return web.Response(status=403, text="access key required: open the link printed by the PC")
        ws = web.WebSocketResponse(max_msg_size=32 << 20)
        await ws.prepare(request)
        peer = request.remote
        hello, ping_task, pending = None, None, {}
        log.info("phone connected from %s", peer)
        try:
            async for msg in ws:
                t_recv = pc_now_ns()
                if msg.type == WSMsgType.BINARY:
                    if hello is None:
                        continue
                    try:
                        h, jpeg = unpack_frame(msg.data)
                    except ValueError as e:
                        log.warning("bad frame: %s", e)
                        continue
                    self.state.on_frame(h, jpeg, t_recv)
                    self.recorder.frame(h, jpeg, t_recv)
                elif msg.type == WSMsgType.TEXT:
                    m = json.loads(msg.data)
                    typ = m.get("type")
                    if typ == "pong":
                        t0 = pending.pop(m.get("id"), None)
                        if t0 is not None:
                            s = SyncSample(m["id"], t0, int(m["t"]), t_recv)
                            self.state.on_sync(s)
                            self.recorder.sync(s)
                    elif typ == "imu":
                        samples = parse_imu_samples(m)
                        self.state.on_imu(samples)
                        self.recorder.imu(samples, t_recv)
                    elif typ == "gnss":
                        self.recorder.gnss(m.get("samples", []), t_recv)
                    elif typ == "status":
                        self.state.on_status(m)
                        self.recorder.status(m, t_recv)
                    elif typ == "hello":
                        hello = {**m, "peer": peer}
                        sid = str(m.get("session_id", "unknown"))
                        self.state.on_connect(sid, hello)
                        self._ws = ws
                        await asyncio.get_running_loop().run_in_executor(None, self._on_session, sid, hello)
                        await ws.send_json({"type": "welcome", "protocol_version": PROTOCOL_VERSION,
                                            "recording": self.recorder.active})
                        if ping_task is None:
                            ping_task = asyncio.create_task(self._ping_loop(ws, pending))
                        log.info("hello: session %s, %s", sid, m.get("user_agent", "")[:80])
                elif msg.type == WSMsgType.ERROR:
                    log.warning("websocket error: %s", ws.exception())
        finally:
            if ping_task:
                ping_task.cancel()
            if self._ws is ws:
                self._ws = None
                self.state.on_disconnect()
                self.recorder.event("disconnect", peer or "")
            log.info("phone disconnected (%s)", peer)
        return ws
