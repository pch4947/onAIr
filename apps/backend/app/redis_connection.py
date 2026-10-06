"""Redis connection lifecycle and bounded readiness check."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from .messaging.consumer import EngineConsumer, stop_consumer
from .streaming.hls import HlsPublisher
from .requests import RequestStore


@asynccontextmanager
async def redis_lifespan(app):
    # Connections open lazily, so the existing prototype APIs remain usable offline.
    client = Redis.from_url(app.state.settings.redis_url, decode_responses=True,
                            socket_connect_timeout=2, socket_timeout=2)
    app.state.redis = client
    app.state.requests = RequestStore(client, app.state.settings.station_id)
    app.state.engine_consumer = EngineConsumer(client, app.state.settings)
    task = asyncio.create_task(app.state.engine_consumer.run(), name="engine-consumer")
    publisher = HlsPublisher(client, app.state.settings)
    app.state.hls = publisher
    try:
        if app.state.settings.hls_enabled:
            await publisher.start()
        yield
    finally:
        await publisher.stop()
        await stop_consumer(task)
        await client.aclose()


async def redis_health(request: Request):
    try:
        async with asyncio.timeout(3):
            await request.app.state.redis.ping()
    except (RedisError, TimeoutError):
        # Do not expose connection URLs, credentials or internal exception messages.
        return JSONResponse(status_code=503, content={"ok": False, "redis": "unavailable"})
    return {"ok": True, "redis": "connected"}
