"""Redis connection lifecycle and bounded readiness check."""

import asyncio
import httpx
from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from .messaging.consumer import EngineConsumer, stop_consumer
from .streaming.hls import HlsPublisher
from .requests import RequestStore
from .users import UserStore
from .stations import StationStore
from .board import BoardStore, CommentStore, LikeStore
from .replays import ReplaySessionStore
from .api import PresenceStore
from .music import MusicService


@asynccontextmanager
async def redis_lifespan(app):
    # Connections open lazily, so the existing prototype APIs remain usable offline.
    client = Redis.from_url(app.state.settings.redis_url, decode_responses=True,
                            socket_connect_timeout=2, socket_timeout=2)
    app.state.redis = client
    app.state.music = MusicService(client, app.state.settings)
    app.state.requests = RequestStore(client, app.state.settings.station_id)
    app.state.users = UserStore(client)
    app.state.stations = StationStore(client, app.state.settings.station_id)
    app.state.board = BoardStore(client)
    app.state.board_comments = CommentStore(client)
    app.state.post_likes = LikeStore(client, "backend:board:post-likes", "backend:board:user-liked-posts")
    app.state.comment_likes = LikeStore(client, "backend:board:comment-likes")
    station_prefix = f"backend:{app.state.settings.station_id}"
    app.state.replay_sessions = ReplaySessionStore(client, app.state.settings.station_id)
    app.state.replay_bookmarks = LikeStore(
        client, f"{station_prefix}:replay-bookmarks", f"{station_prefix}:user-bookmarked-replays")
    app.state.presence = PresenceStore(client, app.state.settings.station_id)
    headers = {'Authorization': f'Bearer {app.state.settings.engine_token}'} if app.state.settings.engine_token else {}
    engine_http = httpx.AsyncClient(base_url=app.state.settings.engine_api_url,
                                   headers=headers, timeout=httpx.Timeout(30, connect=3), trust_env=False)
    app.state.engine_http = engine_http
    app.state.engine_consumer = EngineConsumer(client, app.state.settings)
    task = asyncio.create_task(app.state.engine_consumer.run(), name="engine-consumer")
    publisher = HlsPublisher(client, app.state.settings, app.state.replay_sessions)
    app.state.hls = publisher
    try:
        if app.state.settings.hls_enabled:
            await publisher.start()
        yield
    finally:
        await publisher.stop()
        await stop_consumer(task)
        await client.aclose()
        await engine_http.aclose()


async def redis_health(request: Request):
    try:
        async with asyncio.timeout(3):
            await request.app.state.redis.ping()
    except (RedisError, TimeoutError):
        # Do not expose connection URLs, credentials or internal exception messages.
        return JSONResponse(status_code=503, content={"ok": False, "redis": "unavailable"})
    return {"ok": True, "redis": "connected"}
