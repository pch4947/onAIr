"""Decode the engine envelope (contract v1 and v2, docs/ENGINE_REDIS_CONTRACT.md 10장) and validate audio paths."""

import json
import math
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Text = Annotated[str, Field(min_length=1, max_length=256)]
CONTRACT_VERSIONS = ("1", "2")


class Segment(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    id: Text
    station_id: Text
    audio_ref: Annotated[str, Field(min_length=1, max_length=1024)]
    duration_ms: Annotated[int, Field(gt=0)]
    corner_type: Text
    kind: Literal["speech", "music", "ack", "filler", "glue"]
    priority: Annotated[int, Field(ge=0, le=100)]
    reorderable: bool
    request_ref: Text | None
    music_ref: Text | None
    created_at: Annotated[float, Field(ge=0)]
    # 계약 v2 (10.5절). v1 세그먼트는 둘 다 None이고, v2는 request_ref가 None이다
    input_refs: list[Text] | None = None
    reflection_levels: dict[Text, Annotated[int, Field(ge=1, le=4)]] | None = None


class SegmentV2(BaseModel):
    """v2 segment.submitted — request_ref 대신 input_refs·reflection_levels (10.5절)."""
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    id: Text
    station_id: Text
    audio_ref: Annotated[str, Field(min_length=1, max_length=1024)]
    duration_ms: Annotated[int, Field(gt=0)]
    corner_type: Text
    kind: Literal["speech", "music", "ack", "filler", "glue"]
    priority: Annotated[int, Field(ge=0, le=100)]
    reorderable: bool
    music_ref: Text | None
    created_at: Annotated[float, Field(ge=0)]
    input_refs: Annotated[list[Text], Field(max_length=16)]
    reflection_levels: dict[Text, Annotated[int, Field(ge=1, le=4)]]

    def stored(self) -> Segment:
        if set(self.reflection_levels) != set(self.input_refs):
            raise ValueError("reflection_levels_keys")
        return Segment(**self.model_dump(), request_ref=None)


V1_REQUEST_STATES = ("screened", "queued", "generating", "generated", "rejected", "expired")
# v2에서 엔진이 보내는 입력 상태 (10.4절). collected·held는 아직 보내지 않지만 받는다
V2_REQUEST_STATES = ("pooled", "inboxed", "collected", "held", "not_on_air", "selected", "reflected",
                     "expired", "rejected", "cancelled")


class RequestState(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    request_id: Text
    state: Literal[V1_REQUEST_STATES + V2_REQUEST_STATES]
    # v2 reflected만 싣는다 — 그 입력이 든 세그먼트와 재생 시작 시각 (10.4·10.6절)
    segment_id: Text | None = None
    started_at: Annotated[float, Field(ge=0)] | None = None


class StationStarted(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    topic: str = Field(max_length=60)


class StationRejected(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    reason: Literal['invalid_payload', 'unknown_voice', 'llm_unavailable', 'tts_unavailable']
    detail: str = Field(max_length=4096)


class CornerChanged(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    corner: Text
    title: Text
    contract: dict[Text, Text]
    segment_id: Text
    # 엔진 설정 pipeline.emit_corner_format이 켜져 있을 때만 온다 (10.7절)
    format: Literal['talk', 'music', 'news'] | None = None


class VoteOpen(BaseModel):
    """vote.open (E→B, 10.8절). 기준 세그먼트의 재생이 시작될 때 열고 window_sec 뒤에 닫는다."""
    model_config = ConfigDict(strict=True, extra='forbid')
    vote_id: Text
    kind: Literal['game', 'last_song']
    question: Annotated[str, Field(min_length=1, max_length=200)]
    options: Annotated[list[Annotated[str, Field(min_length=1, max_length=100)]], Field(min_length=2, max_length=2)]
    segment_id: Text
    window_sec: Annotated[int, Field(ge=10, le=600)]


def decode(fields: dict, station_id: str):
    required = {"type", "contract_version", "event_id", "station_id", "at", "payload"}
    if not required <= fields.keys() or not all(isinstance(v, str) for v in fields.values()):
        raise ValueError("invalid_envelope")
    if sum(len(v.encode("utf-8")) for v in fields.values()) > 65536:
        raise ValueError("message_too_large")
    version = fields["contract_version"]
    if version not in CONTRACT_VERSIONS or fields["station_id"] != station_id:
        raise ValueError("version_or_station_mismatch")
    at = float(fields["at"])
    if not math.isfinite(at) or at < 0 or not fields["event_id"]:
        raise ValueError("invalid_event_metadata")
    payload = json.loads(fields["payload"])
    if fields["type"] == "segment.submitted":
        if version == "1" and ({"input_refs", "reflection_levels"} & payload.keys()):
            raise ValueError("v2_fields_in_v1_segment")
        result = Segment.model_validate(payload) if version == "1" else SegmentV2.model_validate(payload).stored()
        if result.station_id != station_id:
            raise ValueError("payload_station_mismatch")
        return result
    if fields["type"] == "request.state":
        result = RequestState.model_validate(payload)
        allowed = V1_REQUEST_STATES if version == "1" else V2_REQUEST_STATES
        if result.state not in allowed or (version == "1" and (result.segment_id or result.started_at is not None)):
            raise ValueError("state_not_in_contract_version")
        return result
    if fields['type'] == 'station.started':
        return StationStarted.model_validate(payload)
    if fields['type'] == 'station.rejected':
        return StationRejected.model_validate(payload)
    if fields['type'] == 'corner.changed':
        return CornerChanged.model_validate(payload)
    if fields['type'] == 'vote.open':
        result = VoteOpen.model_validate(payload)
        if len(set(result.options)) != len(result.options):
            raise ValueError('duplicate_vote_options')
        return result
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
