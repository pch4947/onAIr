"""도메인 모델 — 설계 문서 5장 데이터 모델의 코드 표현.

세그먼트 제출 페이로드(5.1)와 정책 입출력(4.2)이 여기 정의된다.
스키마 변경은 백엔드 파트와의 계약 합의 없이 하지 않는다.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def to_audio_ref(audio_root: Path | str, path: Path | str) -> str:
    """제출 페이로드의 audio_ref — 공유 오디오 루트 기준 POSIX 상대 경로 (설계 문서 5.1).

    백엔드가 URL 경로로 그대로 이어붙일 수 있어야 하므로 OS 경로 구분자에 의존하지 않는다.
    엔진과 백엔드가 같은 루트를 가리킨다는 것이 이 값의 전제다 (확인 1).
    """
    return Path(path).resolve().relative_to(Path(audio_root).resolve()).as_posix()


class SegmentKind(StrEnum):
    SPEECH = "speech"
    MUSIC = "music"
    ACK = "ack"
    FILLER = "filler"


class RequestState(StrEnum):
    """요청 상태 머신 중 엔진 소유 구간 (설계 문서 1장)."""

    SCREENED = "screened"
    QUEUED = "queued"
    GENERATING = "generating"
    GENERATED = "generated"
    REJECTED = "rejected"


@dataclass
class StationProfile:
    """호스트가 방 생성 시 설정하는 AI DJ 스타일 (F-25)."""

    dj_name: str
    tone: str
    concept: str


@dataclass
class StationConfig:
    station_id: str
    profile: StationProfile
    broadcast_minutes: int
    policy_name: str = "naive_fifo"
    recent_segments: int = 8  # 방송 맥락 = 최근 N개 세그먼트 요약 (확인 10 결정)


@dataclass
class ListenerRequest:
    request_id: str
    kind: str  # "story" | "mood"
    body: str
    requester_ref: str
    received_at: float
    state: RequestState = RequestState.SCREENED


@dataclass
class Material:
    """코너가 수집한 소재 (RSS 아이템, 요청 본문, 곡 정보 등)."""

    text: str = ""
    request: ListenerRequest | None = None
    track_id: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Prompt:
    system: str
    user: str


@dataclass
class Script:
    text: str


@dataclass
class GenerationJob:
    job_id: str
    corner_type: str
    material: Material
    priority: int = 50
    reorderable: bool = True


@dataclass
class SegmentSubmission:
    """엔진 -> 백엔드 제출 페이로드 (설계 문서 5.1). 1주차 계약 확정 대상."""

    id: str
    station_id: str
    audio_ref: str
    duration_ms: int
    corner_type: str
    kind: SegmentKind
    priority: int
    reorderable: bool
    request_ref: str | None = None
    music_ref: str | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class ScheduleContext:
    """정책 판단 입력 스냅샷 (설계 문서 4.2).

    실험에서 독립/매개 변수가 될 수 있는 값을 전부 담는 것이 목표.
    """

    now: float
    pending_requests: list[ListenerRequest]
    generated_buffer_sec: float
    inflight_generations: int
    backpressure: bool
    order_position: int
    next_slot: str
    recent_arrival_rate: float = 0.0  # TODO(M4): 최근 창 기반 요청 도착률 실측


@dataclass
class Decision:
    """정책 출력. ack 송출/생략은 1급 필드다 — 생략 결정도 기록된다 (RQ2)."""

    action: str  # "generate" | "wait"
    corner_type: str | None = None
    request: ListenerRequest | None = None
    send_ack: bool = False
