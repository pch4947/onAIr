"""HTTP application and live HLS file serving."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .api import BroadcastState, router
from .config import Settings, load_settings
from .redis_connection import redis_health, redis_lifespan
from fastapi import Request, HTTPException
from fastapi.responses import FileResponse
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
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": cache})

def create_app(settings: Settings | None = None) -> FastAPI:
    application = FastAPI(title="onAIr backend", version="0.1.0", lifespan=redis_lifespan)
    application.state.settings = settings or load_settings()
    application.state.broadcast = BroadcastState()
    application.add_middleware(CORSMiddleware, allow_origins=["*"],
                               allow_methods=["GET", "POST", "OPTIONS"],
                               allow_headers=["Content-Type"])
    application.include_router(router)
    application.add_api_route("/hls/{station_id}/{filename}", hls_file, methods=["GET"])
    application.add_api_route("/health", health, methods=["GET"])
    application.add_api_route("/health/redis", redis_health, methods=["GET"],
                              responses={503: {"description": "Redis unavailable"}})
    return application


async def health() -> dict[str, bool | str]:
    """Process liveness only; Redis readiness is checked separately."""
    return {"ok": True, "service": "onAIr backend"}


app = create_app()
