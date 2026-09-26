"""Decode the engine v1 envelope and validate shared audio paths."""

import json
import math
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Text = Annotated[str, Field(min_length=1, max_length=256)]


class Segment(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    id: Text
    station_id: Text
    audio_ref: Annotated[str, Field(min_length=1, max_length=1024)]
    duration_ms: Annotated[int, Field(gt=0)]
    corner_type: Text
    kind: Literal["speech", "music", "ack", "filler"]
    priority: Annotated[int, Field(ge=0, le=100)]
    reorderable: bool
    request_ref: Text | None
    music_ref: Text | None
    created_at: Annotated[float, Field(ge=0)]


class RequestState(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    request_id: Text
    state: Literal["screened", "queued", "generating", "generated", "rejected"]


def decode(fields: dict, station_id: str):
    required = {"type", "contract_version", "event_id", "station_id", "at", "payload"}
    if not required <= fields.keys() or not all(isinstance(v, str) for v in fields.values()):
        raise ValueError("invalid_envelope")
    if sum(len(v.encode("utf-8")) for v in fields.values()) > 65536:
        raise ValueError("message_too_large")
    if fields["contract_version"] != "1" or fields["station_id"] != station_id:
        raise ValueError("version_or_station_mismatch")
    at = float(fields["at"])
    if not math.isfinite(at) or at < 0 or not fields["event_id"]:
        raise ValueError("invalid_event_metadata")
    payload = json.loads(fields["payload"])
    if fields["type"] == "segment.submitted":
        result = Segment.model_validate(payload)
        if result.station_id != station_id:
            raise ValueError("payload_station_mismatch")
        return result
    if fields["type"] == "request.state":
        return RequestState.model_validate(payload)
    raise ValueError("unsupported_event")


def validate_audio(root: Path, segment: Segment) -> Path:
    ref = segment.audio_ref
    parts = ref.split("/")
    if ("\\" in ref or ":" in ref or "\x00" in ref or PureWindowsPath(ref).is_absolute()
            or any(not p or p.startswith(".") or p.endswith((" ", ".")) for p in parts)
            or parts[0] != segment.station_id):
        raise ValueError("unsafe_audio_ref")
    root = root.resolve()
    path = (root / ref).resolve()
    if not path.is_relative_to(root) or not path.is_relative_to(root / segment.station_id):
        raise ValueError("audio_outside_station")
    if path.suffix.lower() not in {".wav", ".mp3"}:
        raise ValueError("unsupported_audio_extension")
    if not path.is_file():
        raise FileNotFoundError("audio_missing")
    if path.stat().st_size == 0:
        raise ValueError("empty_audio")
    return path
