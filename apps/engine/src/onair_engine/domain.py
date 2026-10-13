"""도메인 모델 — 설계 문서 5장 데이터 모델의 코드 표현.

세그먼트 제출 페이로드(5.1)와 편성 관리자의 판단 기록이 여기 정의된다.
제출 페이로드(SegmentSubmission) 변경은 백엔드 파트와의 계약 합의 없이 하지 않는다.
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
    FILLER = "filler"
    GLUE = "glue"  # 접착 요소(코너 콜·마무리) — 사전 렌더 문구 (glue.py, 백엔드 #103)


class RequestState(StrEnum):
    """요청 상태 머신 중 엔진 소유 구간 (설계 문서 1장)."""

    SCREENED = "screened"
    QUEUED = "queued"
    GENERATING = "generating"
    GENERATED = "generated"
    REJECTED = "rejected"


@dataclass
class StationProfile:
    """호스트가 방 생성 시 설정하는 AI DJ 스타일 (F-25) — persona의 표현 층 (docs/persona.md 2절).

    뒤의 세 필드는 비어 있어도 된다. 비어 있으면 시스템 프롬프트에서 그 블록이 빠진다.
    """

    dj_name: str
    tone: str
    concept: str
    # 대표 멘트 3~5개. 형용사(tone)보다 말투를 강하게 고정한다 (persona.md 4절 2번)
    examples: list[str] = field(default_factory=list)
    # 캐릭터 차원의 금지 사항. 안전 계층(L0~L2)을 대체하지 않는다 (persona.md 5.1절)
    forbidden: list[str] = field(default_factory=list)
    # 가끔 쓰는 입버릇 — 매번 쓰면 반복이 되므로 프롬프트에서 빈도를 제한한다
    signature_phrases: list[str] = field(default_factory=list)


@dataclass
class StationConfig:
    station_id: str
    profile: StationProfile
    broadcast_minutes: int
    # 방송 형식과 호스트 선택 (CORNER_DESIGN 8절, 계약 v2 10.2절) — config/formats/{format}.yaml
    format: str = "talk"
    corners: list[str] | None = None  # talk: 본 코너. None이면 형식 기본값
    news_sections: list[str] | None = None  # news: 분야. None이면 형식 기본값
    intensity: str = "mid"  # 참여 강도 low | mid | high
    recent_segments: int = 8  # 방송 맥락 = 최근 N개 세그먼트 요약 (확인 10 결정)
    # 아래는 station.created로 오는 방 속성 (계약 3.3절). 로컬 설정 파일 실행에서는 비어 있다
    persona_id: str | None = None
    voice: str | None = None  # 이 방의 TTS 보이스. None이면 엔진 기본값(pipeline.tts_voice)
    topic: str | None = None  # 오늘의 주제. None이면 엔진이 정한다
    first_song: dict | None = None  # 계약 3.4절 track — 백엔드가 방송 시작과 함께 튼다
    snapshot: dict | None = None  # 받은 station.created 페이로드 원문 — 계측에 persona 사본으로 남긴다


@dataclass
class ListenerRequest:
    request_id: str
    kind: str  # v1 계약: "story" | "mood" | "song"
    body: str
    requester_ref: str
    received_at: float
    state: RequestState = RequestState.SCREENED


@dataclass
class Material:
    """단계 하나의 소재 — 코너가 꺼낸 항목과, 편성 관리자가 고른 청취자 입력."""

    items: list[dict] = field(default_factory=list)  # 기사·사연·문자 등 (corners/run.py 출처별 형식)
    track: dict | None = None  # 곡 소개면 그 곡 — 계약 3.4절 track. music_ref가 된다
    quotes: list[ListenerRequest] = field(default_factory=list)  # 인용할 청취자 입력
    topic: str | None = None  # 지금 화제 (Thread.topic)
    label: str | None = None  # 뉴스 분야처럼 코너를 다른 소재로 돌릴 때의 이름
    extra: dict = field(default_factory=dict)  # 단계별 부가 정보 (코너 예고 목록, 참여 안내 등)

    @property
    def track_ref(self) -> str | None:
        """music_ref로 나갈 곡. 더미 곡은 백엔드가 틀 수 없으므로 싣지 않는다 (곡 소개 멘트만 나간다)."""
        if not self.track or self.track.get("dummy"):
            return None
        return self.track["track_ref"]


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
    step: str  # 단계 유형 — corners/steps.py
    material: Material
    priority: int = 50
    reorderable: bool = True
    plain: bool = False  # 주관 표현 금지 코너 — persona의 유머·입버릇을 끈다 (CORNER_DESIGN 6.4절)


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
    """편성 관리자의 판단 입력 스냅샷 (ENGINE_ARCHITECTURE 4.1절). decision_log에 그대로 남는다."""

    now: float
    pending_requests: list[ListenerRequest]
    generated_buffer_sec: float
    inflight_generations: int
    backpressure: bool
    corner: str | None  # 진행 중인 코너
    step_index: int  # 그 코너에서 다음에 할 단계
    remaining_sec: float  # 지금까지 낸 오디오가 끝나는 시각부터 방송 종료까지
    # generated_buffer_sec의 근거 — "backend"(backpressure 보고 기준) | "engine"(자체 추정)
    buffer_source: str = "engine"


@dataclass
class Decision:
    """편성 관리자의 출력. 무엇을 만들었는지와 어떤 청취자 입력을 넣었는지 기록한다."""

    action: str  # "generate" | "glue" | "wait" | "idle"(엔딩까지 끝남)
    corner_type: str | None = None
    step: str | None = None
    quotes: list[str] = field(default_factory=list)  # 이 멘트에 넣은 입력의 request_id
