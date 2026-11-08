"""Redis-backed listener requests and prototype broadcast scheduling API."""

from datetime import datetime, timezone
from typing import Annotated, Literal
import asyncio
import json
import time

from fastapi import APIRouter, Body, Depends, Request, HTTPException, Query, Response, Header
from redis.exceptions import RedisError
from pydantic import BaseModel, Field, model_validator

from .auth import get_current_user
from .station_runtime import default_runtime, station_runtime
from .scheduler import decide_next_segment
from .requests import IdempotencyConflict

router = APIRouter(prefix="/api")

PRESENCE_WINDOW_SECONDS = 30


class PresenceStore:
    """하트비트를 보낸 clientId를 ZSET에 시각순으로 저장; 윈도우보다 오래된 건 조회 시 정리한다."""

    def __init__(self, redis, station_id):
        self.redis = redis
        self.key = f"backend:{station_id}:presence"

    async def heartbeat(self, client_id: str, now: float) -> None:
        await self.redis.zadd(self.key, {client_id: now})

    async def count(self, now: float) -> int:
        await self.redis.zremrangebyscore(self.key, 0, now - PRESENCE_WINDOW_SECONDS)
        return await self.redis.zcard(self.key)


class PresenceInput(BaseModel):
    clientId: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


@router.get('/requests/{request_id}/history')
async def request_history(request_id: str, request: Request,
                          offset: Annotated[int, Query(ge=0)] = 0,
                          limit: Annotated[int, Query(ge=1, le=200)] = 100):
    """State transitions observed by the backend; missing engine stages are not invented."""
    try:
        async with asyncio.timeout(3):
            prefix = default_runtime(request.app).engine_consumer.prefix
            entries = await request.app.state.redis.lrange(prefix + ':request-history:' + request_id,
                                                           offset, offset + limit - 1)
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    return {'request_id': request_id, 'history': [json.loads(entry) for entry in entries]}


@router.get('/broadcast/segments/{segment_id}/state')
async def segment_state(segment_id: str, request: Request):
    try:
        async with asyncio.timeout(3):
            raw = await request.app.state.redis.hget(
                default_runtime(request.app).engine_consumer.prefix + ':segment-states', segment_id)
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    if raw is None:
        raise HTTPException(404, 'Segment state not received')
    return json.loads(raw)


@router.get("/broadcast/queue")
async def broadcast_queue(request: Request):
    """Development visibility: queued source audio, not published/playable HLS."""
    prefix = default_runtime(request.app).engine_consumer.prefix
    try:
        async with asyncio.timeout(3):
            ids = await request.app.state.redis.lrange(prefix + ":queue", 0, 99)
            items = await request.app.state.redis.hmget(prefix + ":segments", ids) if ids else []
            count = await request.app.state.redis.llen(prefix + ":queue")
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"station_id": default_runtime(request.app).station_id, "count": count,
            "segments": [json.loads(item) for item in items if item]}


@router.get("/requests/{request_id}/state")
async def engine_request_state(request_id: str, request: Request):
    try:
        async with asyncio.timeout(3):
            entry = await default_runtime(request.app).requests.get(request_id)
            # Preserve lookup of legacy engine-only requests that have no registration record.
            raw = None if entry else await request.app.state.redis.hget(
                default_runtime(request.app).engine_consumer.prefix + ":request-states", request_id)
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    if entry:
        return {"request_id": entry["id"], "state": entry["status"]}
    if raw is None:
        raise HTTPException(404, "Request state not received")
    return json.loads(raw)
MESSAGE_MAX_LENGTH = 200
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveSeconds = Annotated[float, Field(ge=1, allow_inf_nan=False)]


class ListenerInput(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    listenerName: str | None = Field(default=None, min_length=1, max_length=50)
    kind: Literal["story", "chat", "message"] = "story"
    trackTitle: str | None = Field(default=None, min_length=1, max_length=200)
    trackArtist: str | None = Field(default=None, min_length=1, max_length=200)
    trackThumbnailUrl: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def limit_message_length(self):
        if self.kind == "message" and len(self.prompt) > MESSAGE_MAX_LENGTH:
            raise ValueError(f"message must be at most {MESSAGE_MAX_LENGTH} characters")
        return self


class TickInput(BaseModel):
    bufferSeconds: Seconds | None = None
    estimatedGenerationSeconds: Seconds = 30
    bufferTargetSeconds: PositiveSeconds | None = None
    responseWindowSeconds: PositiveSeconds | None = None


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class BroadcastState:
    def __init__(self):
        self.stream = {"status": "bootstrapping", "bufferSeconds": 0,
                       "currentSegment": None, "updatedAt": timestamp()}

    def snapshot(self):
        return {"stream": self.stream}


async def request_metrics(request: Request, required=False):
    try:
        async with asyncio.timeout(3):
            return await default_runtime(request.app).requests.metrics()
    except (RedisError, TimeoutError):
        if required:
            raise HTTPException(503, "Redis unavailable") from None
        # HLS status stays readable during Redis failure; unknown is not zero.
        return {"pendingRequestCount": None, "oldestRequestAgeSeconds": None,
                "requestStatsAvailable": False}


async def viewer_count(request: Request):
    try:
        async with asyncio.timeout(3):
            return await default_runtime(request.app).presence.count(time.time())
    except (RedisError, TimeoutError):
        # Unknown, not zero — same convention as request_metrics above.
        return None


async def station_summary(request: Request) -> tuple[str, str | None]:
    try:
        async with asyncio.timeout(3):
            raw = await request.app.state.redis.get(f'backend:{default_runtime(request.app).station_id}:station')
    except (RedisError, TimeoutError):
        return 'unknown', None
    if not raw:
        return 'idle', None
    record = json.loads(raw)
    # 방 생성 때 형식을 고르지 않으면 config에 format이 빠지고 엔진은 talk로 방송한다.
    return record['status'], record.get('config', {}).get('format', 'talk')


async def current_corner(request: Request) -> dict | None:
    try:
        async with asyncio.timeout(3):
            raw = await request.app.state.redis.get(f'backend:{default_runtime(request.app).station_id}:current-corner')
    except (RedisError, TimeoutError):
        return None
    return json.loads(raw) if raw else None


@router.post("/stations/{station_id}/presence", status_code=204)
async def presence_heartbeat(station_id: str, body: PresenceInput, request: Request):
    station = station_runtime(request, station_id)
    try:
        async with asyncio.timeout(3):
            await station.presence.heartbeat(body.clientId, time.time())
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None


@router.get("/stream/state")
async def stream_state(request: Request, response: Response,
                       fragment: Annotated[str | None, Query(max_length=128,
                           pattern=r"^[a-f0-9]{32}_[a-f0-9]{32}_[0-9]+\.ts$")] = None):
    response.headers["Cache-Control"] = "no-store"
    result = request.app.state.broadcast.snapshot()
    result["stationId"] = default_runtime(request.app).station_id
    # 둘 다 Redis 호출이라 순차 await하면 각자의 연결 타임아웃이 합산돼 버림; 동시에 실행.
    viewers, metrics, (status, station_format), corner = await asyncio.gather(
        viewer_count(request), request_metrics(request), station_summary(request), current_corner(request))
    result['stationStatus'] = status
    result['stationFormat'] = station_format
    result['currentCorner'] = corner if status in ('started', 'draining') else None
    result["viewerCount"] = viewers
    result.update(metrics)
    hls = default_runtime(request.app).hls
    if request.app.state.settings.hls_enabled:
        result["stream"] = {**result["stream"], "status": "ready" if hls.ready else "bootstrapping",
                            "currentSegment": None, "updatedAt": timestamp(),
                            "hlsUrl": f"/hls/{default_runtime(request.app).station_id}/index.m3u8",
                            "sessionId": hls.session, "error": hls.error}
        result['stream'].update(hls.buffer_snapshot())
        result['stream']['error'] = hls.error or hls.redis_error
    if fragment is not None:
        playback = hls.playback_metadata(fragment) if request.app.state.settings.hls_enabled else {
            "fragment": fragment, "status": "unavailable", "segment": None}
        result["stream"] = {**result["stream"], "currentSegment": playback["segment"], "playback": playback}
    if result['stationStatus'] in ('ended', 'cleaning', 'stopping', 'rejected'):
        result['stream'] = {**result['stream'], 'status': 'bootstrapping', 'hlsUrl': None, 'currentSegment': None}
    return result


def public_request(entry: dict, user: dict) -> dict:
    """Listeners learn only whether an entry is their own, so an anonymous one stays anonymous."""
    return {key: value for key, value in entry.items() if key != "listenerId"} | {
        "mine": entry["listenerId"] == user["id"]}


@router.get("/requests")
async def list_requests(request: Request, user: Annotated[dict, Depends(get_current_user)]):
    try:
        async with asyncio.timeout(3):
            entries = await default_runtime(request.app).requests.list()
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"requests": [public_request(entry, user) for entry in entries]}


@router.post("/requests", status_code=202)
async def create_request(body: ListenerInput, request: Request,
                         user: Annotated[dict, Depends(get_current_user)],
                         idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128,
                             pattern=r"^[A-Za-z0-9._:-]+$")] = None):
    """Persist a request and enqueue request.arrived together; 202 is not engine completion.

    The listener comes from the token, never the body. Omitting listenerName stays anonymous on air.
    """
    try:
        async with asyncio.timeout(3):
            entry = await default_runtime(request.app).requests.create(
                body.prompt, user["id"], body.kind, idempotency_key,
                listener_name=user["name"] if body.listenerName else None, track_title=body.trackTitle,
                track_artist=body.trackArtist, track_thumbnail_url=body.trackThumbnailUrl)
    except IdempotencyConflict:
        raise HTTPException(409, "Idempotency-Key already used with different input") from None
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable; retry with the same Idempotency-Key") from None
    return {"request": public_request(entry, user)}


@router.post("/scheduler/tick")
async def tick(request: Request, body: TickInput = Body(default_factory=TickInput)):
    state = request.app.state.broadcast
    settings = request.app.state.settings
    metrics = await request_metrics(request, required=True)
    buffer = (default_runtime(request.app).hls.buffer_snapshot()['bufferSeconds'] if settings.hls_enabled
              else state.stream['bufferSeconds']) if body.bufferSeconds is None else body.bufferSeconds
    decision = decide_next_segment(
        buffer_seconds=buffer, estimated_generation_seconds=body.estimatedGenerationSeconds,
        oldest_request_age_seconds=metrics["oldestRequestAgeSeconds"],
        pending_request_count=metrics["pendingRequestCount"],
        buffer_target_seconds=body.bufferTargetSeconds or settings.buffer_target_seconds,
        response_window_seconds=body.responseWindowSeconds or settings.response_window_seconds,
    )
    state.stream = {**state.stream, "status": "ready" if decision["action"] == "idle" else "planning",
                    "bufferSeconds": buffer, "updatedAt": timestamp()}
    return {"decision": decision, **state.snapshot(), **metrics}
