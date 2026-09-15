"""HTTP application. Streaming resources will be added in later steps."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .api import BroadcastState, router
from .config import Settings, load_settings

def create_app(settings: Settings | None = None) -> FastAPI:
    application = FastAPI(title="onAIr backend", version="0.1.0")
    application.state.settings = settings or load_settings()
    application.state.broadcast = BroadcastState()
    application.add_middleware(CORSMiddleware, allow_origins=["*"],
                               allow_methods=["GET", "POST", "OPTIONS"],
                               allow_headers=["Content-Type"])
    application.include_router(router)
    application.add_api_route("/health", health, methods=["GET"])
    return application


async def health() -> dict[str, bool | str]:
    """Process liveness only; Redis and streaming are not connected yet."""
    return {"ok": True, "service": "onAIr backend"}


app = create_app()
