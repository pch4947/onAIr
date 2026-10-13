"""HTTP application and live HLS file serving."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .api import BroadcastState, router
from .auth import router as auth_router
from .board import router as board_router
from .notifications import router as notifications_router
from .stations import router as stations_router
from .replays import router as replays_router
from .music import router as music_router
from .config import Settings, load_settings
from .redis_connection import redis_health, redis_lifespan
from fastapi import Request, HTTPException
from fastapi.responses import FileResponse, Response
import re


async def hls_file(station_id: str, filename: str, request: Request):
    settings = request.app.state.settings
    publisher = request.app.state.hls
    if not settings.hls_enabled or station_id != settings.station_id:
        raise HTTPException(404, "Station not found")
    if filename == "index.m3u8":
        if not publisher.ready:
            raise HTTPException(503, "Stream warming up", headers={"Retry-After": "2"})
        media_type = "application/vnd.apple.mpegurl"
        cache = "no-store"
    elif re.fullmatch(r"[a-f0-9]{32}_[a-f0-9]{32}_[0-9]+\.ts", filename):
        media_type, cache = "video/mp2t", "public, max-age=60"
    else:
        raise HTTPException(404, "HLS file not found")
    path = publisher.directory / filename
    if not path.is_file():
        raise HTTPException(404, "HLS file not found")
    if filename == 'index.m3u8':
        # This tiny live file is replaced frequently. Snapshot it before yielding
        # so Content-Length and body always describe the same playlist version.
        return Response(path.read_bytes(), media_type=media_type, headers={'Cache-Control': cache})
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": cache})


async def vod_file(station_id: str, session_id: str, filename: str, request: Request):
    settings = request.app.state.settings
    publisher = request.app.state.hls
    if not settings.hls_enabled or station_id != settings.station_id:
        raise HTTPException(404, "Station not found")
    if not re.fullmatch(r"[a-f0-9]{32}", session_id):
        raise HTTPException(404, "Replay not found")
    if filename == "index.m3u8":
        media_type, cache = "application/vnd.apple.mpegurl", "no-store"
    elif re.fullmatch(r"[a-f0-9]{32}_[a-f0-9]{32}_[0-9]+\.ts", filename):
        media_type, cache = "video/mp2t", "public, max-age=31536000, immutable"
    else:
        raise HTTPException(404, "VOD file not found")
    path = publisher.directory / "vod" / session_id / filename
    if not path.is_file():
        raise HTTPException(404, "VOD file not found")
    if filename == 'index.m3u8':
        # 세션이 아직 라이브인 동안은 계속 커지는 파일이라 라이브 플레이리스트처럼 스냅샷으로 응답.
        return Response(path.read_bytes(), media_type=media_type, headers={'Cache-Control': cache})
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": cache})


def create_app(settings: Settings | None = None) -> FastAPI:
    application = FastAPI(title="onAIr backend", version="0.1.0", lifespan=redis_lifespan)
    application.state.settings = settings or load_settings()
    application.state.broadcast = BroadcastState()
    application.add_middleware(CORSMiddleware, allow_origins=["*"],
                               allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
                               allow_headers=["Content-Type", "Idempotency-Key", "Authorization"])
    application.include_router(router)
    application.include_router(auth_router)
    application.include_router(board_router)
    application.include_router(notifications_router)
    application.include_router(stations_router)
    application.include_router(replays_router)
    application.include_router(music_router)
    application.add_api_route("/hls/{station_id}/{filename}", hls_file, methods=["GET"])
    application.add_api_route("/hls/{station_id}/vod/{session_id}/{filename}", vod_file, methods=["GET"])
    application.add_api_route("/health", health, methods=["GET"])
    application.add_api_route("/health/redis", redis_health, methods=["GET"],
                              responses={503: {"description": "Redis unavailable"}})
    return application


async def health() -> dict[str, bool | str]:
    """Process liveness only; Redis readiness is checked separately."""
    return {"ok": True, "service": "onAIr backend"}


app = create_app()
