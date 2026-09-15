"""Prototype request/state API. State belongs to one app process."""

from datetime import datetime, timezone
from math import floor
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, Field

from .scheduler import decide_next_segment

router = APIRouter(prefix="/api")
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveSeconds = Annotated[float, Field(ge=1, allow_inf_nan=False)]


class ListenerInput(BaseModel):
    prompt: str = Field(min_length=1)
    listenerId: str | None = None


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
        self.requests = []

    def snapshot(self):
        age = 0
        if self.requests:
            created = datetime.fromisoformat(self.requests[0]["createdAt"])
            age = max(0, floor((datetime.now(timezone.utc) - created).total_seconds() + 0.5))
        return {"stream": self.stream, "pendingRequestCount": len(self.requests),
                "oldestRequestAgeSeconds": age}


@router.get("/stream/state")
async def stream_state(request: Request):
    return request.app.state.broadcast.snapshot()


@router.get("/requests")
async def list_requests(request: Request):
    return {"requests": request.app.state.broadcast.requests}


@router.post("/requests", status_code=202)
async def create_request(body: ListenerInput, request: Request):
    entry = {"id": str(uuid4()), "listenerId": body.listenerId if body.listenerId is not None else "anonymous",
             "prompt": body.prompt, "status": "queued", "createdAt": timestamp()}
    request.app.state.broadcast.requests.append(entry)
    return {"request": entry}


@router.post("/scheduler/tick")
async def tick(request: Request, body: TickInput = Body(default_factory=TickInput)):
    state = request.app.state.broadcast
    settings = request.app.state.settings
    buffer = state.stream["bufferSeconds"] if body.bufferSeconds is None else body.bufferSeconds
    decision = decide_next_segment(
        buffer_seconds=buffer, estimated_generation_seconds=body.estimatedGenerationSeconds,
        oldest_request_age_seconds=state.snapshot()["oldestRequestAgeSeconds"],
        pending_request_count=len(state.requests),
        buffer_target_seconds=body.bufferTargetSeconds or settings.buffer_target_seconds,
        response_window_seconds=body.responseWindowSeconds or settings.response_window_seconds,
    )
    state.stream = {**state.stream, "status": "ready" if decision["action"] == "idle" else "planning",
                    "bufferSeconds": buffer, "updatedAt": timestamp()}
    return {"decision": decision, **state.snapshot()}
