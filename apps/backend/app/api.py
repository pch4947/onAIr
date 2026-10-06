"""Redis-backed listener requests and prototype broadcast scheduling API."""

from datetime import datetime, timezone
from typing import Annotated, Literal
import asyncio
import json

from fastapi import APIRouter, Body, Request, HTTPException, Query, Response, Header
from redis.exceptions import RedisError
from pydantic import BaseModel, Field

from .scheduler import decide_next_segment
from .requests import IdempotencyConflict

router = APIRouter(prefix="/api")


@router.get('/requests/{request_id}/history')
async def request_history(request_id: str, request: Request,
                          offset: Annotated[int, Query(ge=0)] = 0,
                          limit: Annotated[int, Query(ge=1, le=200)] = 100):
    """State transitions observed by the backend; missing engine stages are not invented."""
    try:
        async with asyncio.timeout(3):
            prefix = request.app.state.engine_consumer.prefix
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
                request.app.state.engine_consumer.prefix + ':segment-states', segment_id)
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    if raw is None:
        raise HTTPException(404, 'Segment state not received')
    return json.loads(raw)


@router.get("/broadcast/queue")
async def broadcast_queue(request: Request):
    """Development visibility: queued source audio, not published/playable HLS."""
    prefix = request.app.state.engine_consumer.prefix
    try:
        async with asyncio.timeout(3):
            ids = await request.app.state.redis.lrange(prefix + ":queue", 0, 99)
            items = await request.app.state.redis.hmget(prefix + ":segments", ids) if ids else []
            count = await request.app.state.redis.llen(prefix + ":queue")
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"station_id": request.app.state.settings.station_id, "count": count,
            "segments": [json.loads(item) for item in items if item]}


@router.get("/requests/{request_id}/state")
async def engine_request_state(request_id: str, request: Request):
    try:
        async with asyncio.timeout(3):
            entry = await request.app.state.requests.get(request_id)
            # Preserve lookup of legacy engine-only requests that have no registration record.
            raw = None if entry else await request.app.state.redis.hget(
                request.app.state.engine_consumer.prefix + ":request-states", request_id)
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    if entry:
        return {"request_id": entry["id"], "state": entry["status"]}
    if raw is None:
        raise HTTPException(404, "Request state not received")
    return json.loads(raw)
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveSeconds = Annotated[float, Field(ge=1, allow_inf_nan=False)]


class ListenerInput(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    listenerId: str | None = Field(default=None, min_length=1, max_length=256)
    kind: Literal["story", "mood"] = "story"


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
            return await request.app.state.requests.metrics()
    except (RedisError, TimeoutError):
        if required:
            raise HTTPException(503, "Redis unavailable") from None
        # HLS status stays readable during Redis failure; unknown is not zero.
        return {"pendingRequestCount": None, "oldestRequestAgeSeconds": None,
                "requestStatsAvailable": False}


@router.get("/stream/state")
async def stream_state(request: Request, response: Response,
                       fragment: Annotated[str | None, Query(max_length=128,
                           pattern=r"^[a-f0-9]{32}_[a-f0-9]{32}_[0-9]+\.ts$")] = None):
    response.headers["Cache-Control"] = "no-store"
    result = request.app.state.broadcast.snapshot()
    result.update(await request_metrics(request))
    hls = request.app.state.hls
    if request.app.state.settings.hls_enabled:
        result["stream"] = {**result["stream"], "status": "ready" if hls.ready else "bootstrapping",
                            "currentSegment": None, "updatedAt": timestamp(),
                            "hlsUrl": f"/hls/{request.app.state.settings.station_id}/index.m3u8",
                            "error": hls.error}
        result['stream'].update(hls.buffer_snapshot())
        result['stream']['error'] = hls.error or hls.redis_error
    if fragment is not None:
        playback = hls.playback_metadata(fragment) if request.app.state.settings.hls_enabled else {
            "fragment": fragment, "status": "unavailable", "segment": None}
        result["stream"] = {**result["stream"], "currentSegment": playback["segment"], "playback": playback}
    return result


@router.get("/requests")
async def list_requests(request: Request):
    try:
        async with asyncio.timeout(3):
            return {"requests": await request.app.state.requests.list()}
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None


@router.post("/requests", status_code=202)
async def create_request(body: ListenerInput, request: Request,
                         idempotency_key: Annotated[str | None, Header(min_length=1, max_length=128,
                             pattern=r"^[A-Za-z0-9._:-]+$")] = None):
    """Persist a request and enqueue request.arrived together; 202 is not engine completion."""
    try:
        async with asyncio.timeout(3):
            entry = await request.app.state.requests.create(
                body.prompt, body.listenerId or "anonymous", body.kind, idempotency_key)
    except IdempotencyConflict:
        raise HTTPException(409, "Idempotency-Key already used with different input") from None
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable; retry with the same Idempotency-Key") from None
    return {"request": entry}


@router.post("/scheduler/tick")
async def tick(request: Request, body: TickInput = Body(default_factory=TickInput)):
    state = request.app.state.broadcast
    settings = request.app.state.settings
    metrics = await request_metrics(request, required=True)
    buffer = (request.app.state.hls.buffer_snapshot()['bufferSeconds'] if settings.hls_enabled
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
