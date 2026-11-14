"""도메인 모델 — 설계 문서 5장 데이터 모델의 코드 표현.

세그먼트 제출 페이로드(5.1)와 편성 관리자의 판단 기록이 여기 정의된다.
제출 페이로드(SegmentSubmission) 변경은 백엔드 파트와의 계약 합의 없이 하지 않는다.
"""
from __future__ import annotations

import asyncio
import dataclasses
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
    EXPIRED = "expired"
    # 계약 v1 백엔드가 받지 않는 값 — wire_state로 바꿔 보내거나 계측에만 남긴다. v2는 그대로 보낸다 (계약 10.4절)
    INBOXED = "inboxed"
    NOT_ON_AIR = "not_on_air"
    CANCELLED = "cancelled"
    REFLECTED = "reflected"


_WIRE_STATES = {RequestState.INBOXED: RequestState.QUEUED, RequestState.NOT_ON_AIR: RequestState.EXPIRED,
                RequestState.CANCELLED: None, RequestState.REFLECTED: None}


# 계약 v2의 request.state 값 (10.4절). 엔진 안의 이름은 v1 그대로 두고 보낼 때 바꾼다.
# screened·generated는 v2에 없다 — 바로 뒤에 pooled/inboxed가, 재생이 시작되면 reflected가 나간다
_WIRE_STATES_V2 = {RequestState.SCREENED: None, RequestState.QUEUED: "pooled",
                   RequestState.GENERATING: "selected", RequestState.GENERATED: None}


def wire_state(state: RequestState, version: int = 1) -> str | None:
    """백엔드에 보낼 값. None이면 보내지 않는다 — 모르는 값은 백엔드가 이벤트째 격리한다."""
    if version >= 2:
        return _WIRE_STATES_V2.get(state, str(state))
    return _WIRE_STATES.get(state, state)


class ChatType(StrEnum):
    """채팅 유형 (CORNER_DESIGN 5.1.3절)."""

    REACTION = "reaction"  # 지금 나오는 곡·사연·화제에 대한 반응 — 늦으면 의미가 없다
    STANDALONE = "standalone"  # 의견·근황·인사 — 언제 읽어도 뜻이 같다


@dataclass(frozen=True)
class Anchor:
    """반응의 대상 — 곡, 사연·문자, 코너 화제. seq는 생성 순서다."""

    id: str
    kind: str  # track | story | message | topic
    keywords: frozenset[str]
    seq: int


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
    experiment: dict | None = None  # 실험용 방의 조건 (ExperimentSpec) — selector, window_sec


@dataclass
class ListenerRequest:
    request_id: str
    kind: str  # chat | message | song_request | board_story — v1의 story·mood는 chat으로 받는다
    body: str
    requester_ref: str
    received_at: float
    state: RequestState = RequestState.SCREENED
    nickname: str | None = None  # 방송에서 부를 이름. L0를 통과한 값만 들어온다
    tags: list[str] = field(default_factory=list)  # 게시판 사연의 주제 태그
    chat_type: ChatType | None = None
    rule: str | None = None  # 유형을 정한 규칙 — 분류 정확도 측정용
    anchor_id: str | None = None  # 도착 순간 방송에 나가고 있던 것
    anchor_seq: int | None = None


@dataclass
class Material:
    """단계 하나의 소재 — 코너가 꺼낸 항목과, 편성 관리자가 고른 청취자 입력."""

    items: list[dict] = field(default_factory=list)  # 기사·사연·문자 등 (corners/run.py 출처별 형식)
    track: dict | None = None  # 곡 소개면 그 곡 — 계약 3.4절 track. music_ref가 된다
    quotes: list[ListenerRequest] = field(default_factory=list)  # 인용할 청취자 입력
    topic: str | None = None  # 지금 화제 (Thread.topic)
    label: str | None = None  # 뉴스 분야처럼 코너를 다른 소재로 돌릴 때의 이름
    extra: dict = field(default_factory=dict)  # 단계별 부가 정보 (코너 예고 목록, 참여 안내 등)
    feature_ref: str | None = None  # 이 단계의 주 소재인 입력 — 성공하면 request_ref로 나간다
    must_read: str | None = None  # 대본에 반드시 들어가야 할 글 (문자 낭독)

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


class BlockScript:
    """한 번에 쓴 이야기 덩어리 — 첫 문단을 맡은 작업이 LLM을 부르고, 나머지 문단 작업은 그 결과를 기다린다.

    곡 사이 말을 멘트마다 따로 쓰면 각자 시작과 끝이 있는 토막이 된다. 덩어리로 쓰고 문단 단위로 합성하면
    글은 이어지고 합성 지연은 멘트 하나와 같다 (CORNER_DESIGN 7.7.1절, #262).
    """

    WAIT_SEC = 90.0  # 첫 문단 작업이 결과를 내지 못했을 때 나머지가 기다리는 상한

    def __init__(self, parts: int):
        self.parts = parts  # 계획한 문단 수 — 실제로 나온 문단이 적으면 남는 작업은 건너뛴다
        self._texts: asyncio.Future[list[str] | None] = asyncio.get_running_loop().create_future()

    def resolve(self, texts: list[str]) -> None:
        if not self._texts.done():
            self._texts.set_result(texts)

    def fail(self) -> None:
        """대본을 쓰지 못했다 — 기다리는 작업을 풀어 준다. 이미 결과가 있으면 그대로 둔다."""
        if not self._texts.done():
            self._texts.set_result(None)

    async def wait(self) -> list[str] | None:
        try:
            return await asyncio.wait_for(asyncio.shield(self._texts), self.WAIT_SEC)
        except TimeoutError:
            return None


@dataclass
class GenerationJob:
    job_id: str
    corner_type: str
    step: str  # 단계 유형 — corners/steps.py
    material: Material
    priority: int = 50
    reorderable: bool = True
    plain: bool = False  # 주관 표현 금지 코너 — persona의 유머·입버릇을 끈다 (CORNER_DESIGN 6.4절)
    anchor: Anchor | None = None  # 이 세그먼트가 다루는 것 — 제출 페이로드에는 싣지 않는다
    script_chars: int = 0  # 대본 글자 수 — 인용 점유율 원시 값. 파이프라인이 채운다
    provider_failed: bool = False  # LLM·TTS 장애로 대체됐다 — 인용한 입력의 잘못이 아니다
    feature: ListenerRequest | None = None  # 주 소재인 입력 — 문자·사연·신청곡
    block: BlockScript | None = None  # 이야기 덩어리의 한 문단이면 그 덩어리
    part: int = 0  # 덩어리의 몇 번째 문단인가 — 0번 작업이 덩어리 전체를 쓴다


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
    # 계약 v2 (10.5절) — 이 멘트가 반영한 입력들과 입력별 반영 단계(1~4). v1로 보낼 때는 싣지 않는다
    input_refs: list[str] = field(default_factory=list)
    reflection_levels: dict[str, int] = field(default_factory=dict)


QUOTE_LEVEL = 2  # 기획서 4.2절 ② — 인용·낭독


def segment_payload(sub: SegmentSubmission, version: int = 1) -> dict:
    """segment.submitted의 payload. v1은 request_ref 하나, v2는 input_refs와 reflection_levels (계약 10.5절)."""
    payload = dataclasses.asdict(sub)
    refs, levels = payload.pop("input_refs"), payload.pop("reflection_levels")
    if version < 2:
        return payload
    ref = payload.pop("request_ref")
    if not refs and ref:  # 반영 단계를 따로 정하지 않은 제출 — 인용으로 본다
        refs, levels = [ref], {ref: QUOTE_LEVEL}
    payload["input_refs"] = refs
    payload["reflection_levels"] = {r: levels.get(r, QUOTE_LEVEL) for r in refs}
    return payload


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

    action: str  # "generate" | "glue" | "wait" | "tts_blocked"(한도 초과) | "idle"(엔딩까지 끝남)
    corner_type: str | None = None
    step: str | None = None
    quotes: list[str] = field(default_factory=list)  # 이 멘트에 넣은 입력의 request_id
    situation: str | None = None  # 채팅 상황 quiet | normal | flood (선별했을 때만)
    criterion: str | None = None  # 선별 기준 (선별했을 때만)
    # 이야기 단계의 소재 — chat | callback(다시 꺼내기) | angle(갈래의 말할 거리) | topic(주제만) (CORNER_DESIGN 7.7절)
    material: str | None = None
