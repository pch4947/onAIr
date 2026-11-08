"""Redis connection lifecycle and bounded readiness check."""

import asyncio
import json
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
from .board_stories import StoryFeed
from .notifications import NotificationStore
from .replays import ReplaySessionStore
from .api import PresenceStore
from .music import MusicService
from .station_lifecycle import StationLifecycle
from .station_runtime import ensure_runtimes


def open_station(station, client):
    """방송 하나의 저장소와 작업자를 만든다. 작업자 task는 호출한 쪽이 띄운다."""
    settings, sid = station.settings, station.station_id
    station.music = MusicService(client, settings)
    station.requests = RequestStore(client, sid)
    station.stations = StationStore(client, sid)
    station.story_feed = StoryFeed(client, sid)
    station.replay_sessions = ReplaySessionStore(client, sid)
    station.replay_bookmarks = LikeStore(
        client, f"backend:{sid}:replay-bookmarks", f"backend:{sid}:user-bookmarked-replays")
    station.presence = PresenceStore(client, sid)
    station.engine_consumer = EngineConsumer(client, settings)
    station.engine_consumer.state = station
    station.hls = HlsPublisher(client, settings, station.replay_sessions)
    station.lifecycle = StationLifecycle(station)
    station.engine_consumer.lifecycle_lock = station.lifecycle.lock


async def station_status(client, station):
    try:
        raw = await client.get(station.stations.key)
    except RedisError:
        return 'unknown'  # Keep health endpoints available; retry from lifecycle.
    return json.loads(raw)['status'] if raw else None


@asynccontextmanager
async def redis_lifespan(app):
    # Connections open lazily, so the existing prototype APIs remain usable offline.
    client = Redis.from_url(app.state.settings.redis_url, decode_responses=True,
                            socket_connect_timeout=2, socket_timeout=2)
    app.state.redis = client
    app.state.users = UserStore(client)
    app.state.board = BoardStore(client)
    app.state.board_comments = CommentStore(client)
    app.state.post_likes = LikeStore(client, "backend:board:post-likes", "backend:board:user-liked-posts")
    app.state.comment_likes = LikeStore(client, "backend:board:comment-likes")
    app.state.notifications = NotificationStore(client)
    headers = {'Authorization': f'Bearer {app.state.settings.engine_token}'} if app.state.settings.engine_token else {}
    engine_http = httpx.AsyncClient(base_url=app.state.settings.engine_api_url,
                                   headers=headers, timeout=httpx.Timeout(30, connect=3), trust_env=False)
    app.state.engine_http = engine_http
    stations = list(ensure_runtimes(app.state).values())
    consumer_tasks, lifecycle_tasks = [], []
    try:
        for station in stations:
            open_station(station, client)
            consumer_tasks.append(asyncio.create_task(
                station.engine_consumer.run(), name=f"engine-consumer-{station.station_id}"))
        try:
            await StoryFeed(client, app.state.default_station_id).stories_since()
        except RedisError:
            pass  # Recorded on the first story offer instead.
        for station in stations:
            status = await station_status(client, station)
            if station.settings.hls_enabled and status not in ('ended', 'cleaning', 'unknown'):
                await station.hls.start()
            lifecycle_tasks.append(asyncio.create_task(
                station.lifecycle.run(), name=f'station-lifecycle-{station.station_id}'))
        yield
    finally:
        for task in lifecycle_tasks:
            await stop_consumer(task)
        for station in stations:
            if station.hls:
                await station.hls.stop()
        for task in consumer_tasks:
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
