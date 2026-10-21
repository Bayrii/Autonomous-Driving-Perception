"""FastAPI app: serves the phone page and the recording API from one origin."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import jpeg
from .models import FrameMeta, ImuBatch, SessionCreate, StopRequest
from .settings import Settings
from .storage import SessionStore, StoreError

log = logging.getLogger("phonerec")
STATIC = Path(__file__).parent / "static"
SessionId = Annotated[str, PathParam(pattern=r"^[0-9a-f]{32}$")]

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; "
       "media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")


def _unlink(path: Path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _validation_message(err: ValidationError) -> str:
    parts = [f"{'.'.join(str(x) for x in e['loc']) or 'body'}: {e['msg']}" for e in err.errors()[:5]]
    return "Invalid request: " + "; ".join(parts)


async def read_limited(request: Request, limit: int) -> bytes:
    """Read the request body, refusing anything over ``limit`` bytes without buffering it."""
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > limit:
        raise HTTPException(413, f"Request body too large ({cl} bytes, limit {limit}).")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(413, f"Request body too large (limit {limit} bytes).")
    return bytes(buf)


def parse(model: type[BaseModel], body: bytes):
    try:
        return model.model_validate_json(body or b"{}")
    except ValidationError as e:
        raise HTTPException(422, _validation_message(e))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = SessionStore(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.auth_disabled:
            log.warning("APP_AUTH_DISABLED is set: recording endpoints are open to anyone. Local use only.")
        elif not settings.access_token:
            log.warning("APP_ACCESS_TOKEN is not set: recording endpoints will refuse all requests (503).")

        async def cleanup_loop():
            while True:
                try:
                    rep = await run_in_threadpool(store.cleanup)
                    if any(rep.values()):
                        log.info("cleanup: %s", rep)
                except Exception:  # never let cleanup kill the app
                    log.exception("cleanup failed")
                await asyncio.sleep(settings.cleanup_interval_s)

        task = asyncio.create_task(cleanup_loop())
        yield
        task.cancel()

    app = FastAPI(title="Phone camera + IMU recorder", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.store = settings, store

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        else:
            resp.headers.setdefault("Content-Security-Policy", CSP)
        return resp

    @app.exception_handler(StoreError)
    async def store_error(request: Request, exc: StoreError):
        return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=exc.status)

    @app.exception_handler(OSError)
    async def disk_error(request: Request, exc: OSError):
        log.exception("storage error on %s", request.url.path)
        return JSONResponse({"detail": "The server could not write the recording to disk. Try again; if it "
                                       "persists, check the server logs.", "code": "storage_error"}, status_code=500)

    def require_token(request: Request) -> None:
        if settings.auth_disabled:
            return
        if not settings.access_token:
            raise HTTPException(503, "The server has no APP_ACCESS_TOKEN configured. Set it in the Render "
                                     "dashboard (Environment) and redeploy.")
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(token.strip().encode(),
                                                                   settings.access_token.encode()):
            raise HTTPException(401, "Missing or wrong access token.", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(require_token)]

    # ------------------------------------------------------------------ public
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/config")
    async def client_config():
        return settings.client_config()

    # ------------------------------------------------------------------ protected
    @app.get("/api/auth/check", dependencies=auth)
    async def auth_check():
        return {"ok": True}

    @app.post("/api/sessions", dependencies=auth, status_code=201)
    async def create_session(request: Request):
        req = parse(SessionCreate, await read_limited(request, settings.max_json_bytes))
        sess = await run_in_threadpool(store.create, req)
        return {"session_id": sess.id, "status": sess.status, "created_utc": sess.meta["created_utc"],
                "limits": {"max_session_seconds": settings.max_session_seconds,
                           "max_frame_bytes": settings.max_frame_bytes, "max_imu_batch": settings.max_imu_batch}}

    @app.get("/api/sessions", dependencies=auth)
    async def list_sessions():
        return {"sessions": store.list(), "total_bytes": store.total_bytes,
                "max_total_bytes": settings.max_total_bytes,
                "storage_note": "Temporary storage: recordings are lost when the server restarts or redeploys."}

    @app.get("/api/sessions/{sid}", dependencies=auth)
    async def get_session(sid: SessionId):
        return store.get(sid).summary(settings.session_ttl_s)

    @app.post("/api/sessions/{sid}/frames", dependencies=auth)
    async def upload_frame(sid: SessionId, request: Request, meta: Annotated[FrameMeta, Query()]):
        store.get(sid)  # 404 before reading the body
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "image/jpeg":
            raise HTTPException(415, "Frames must be sent as the raw JPEG body with Content-Type: image/jpeg.")
        body = await read_limited(request, settings.max_frame_bytes)
        try:
            w, h = jpeg.jpeg_dimensions(body)
            if not (settings.min_frame_side <= w <= settings.max_frame_side
                    and settings.min_frame_side <= h <= settings.max_frame_side):
                raise jpeg.JpegError(f"image {w}x{h} outside the allowed size "
                                     f"({settings.min_frame_side}-{settings.max_frame_side} px per side)")
            if (w, h) != (meta.width, meta.height):
                raise jpeg.JpegError(f"metadata says {meta.width}x{meta.height} but the JPEG is {w}x{h}")
            if settings.validate_jpeg_decode and not await run_in_threadpool(jpeg.decodes, body):
                raise jpeg.JpegError("JPEG data could not be decoded")
        except jpeg.JpegError as e:
            store.count_rejected(sid, "frames")
            raise HTTPException(422, f"Invalid frame: {e}")
        status = await run_in_threadpool(store.add_frame, sid, meta, body, w, h)
        return {"status": status, "seq": meta.seq}

    @app.post("/api/sessions/{sid}/imu", dependencies=auth)
    async def upload_imu(sid: SessionId, request: Request):
        store.get(sid)
        body = await read_limited(request, settings.max_json_bytes)
        try:
            batch = ImuBatch.model_validate_json(body or b"{}")
        except ValidationError as e:
            store.count_rejected(sid, "imu_batches")
            raise HTTPException(422, _validation_message(e))
        if len(batch.samples) > settings.max_imu_batch:
            store.count_rejected(sid, "imu_batches")
            raise HTTPException(413, f"Too many samples in one batch ({len(batch.samples)}, "
                                     f"limit {settings.max_imu_batch}).")
        status, n = await run_in_threadpool(store.add_imu, sid, batch)
        return {"status": status, "batch_seq": batch.batch_seq, "samples_stored": n}

    @app.post("/api/sessions/{sid}/stop", dependencies=auth)
    async def stop_session(sid: SessionId, request: Request):
        req = parse(StopRequest, await read_limited(request, settings.max_json_bytes))
        return await run_in_threadpool(store.stop, sid, req)

    @app.get("/api/sessions/{sid}/download", dependencies=auth)
    async def download(sid: SessionId):
        path, filename = await run_in_threadpool(store.export_zip, sid)
        return FileResponse(path, media_type="application/zip", filename=filename,
                            background=BackgroundTask(_unlink, path))

    @app.delete("/api/sessions/{sid}", dependencies=auth)
    async def delete_session(sid: SessionId):
        await run_in_threadpool(store.delete, sid)
        return {"deleted": sid}

    return app
