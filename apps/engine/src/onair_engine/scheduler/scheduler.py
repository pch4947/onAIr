"""편성 관리자 — 유일한 의사결정 지점 (ENGINE_ARCHITECTURE 3.1, 4.1절).

LLM은 무엇을 말할지 결정하지 않는다. 클락이 코너를, 코너가 단계를 정하고 LLM은 칸을 채운다.
편성 관리자에게 남은 일은 "지금 만들지"(버퍼 관리)와 "어떤 청취자 입력을 넣을지"다.

제출 순서는 편성 순서다 — 생성은 병렬로 하지만, 먼저 끝난 멘트가 앞 순서를 새치기하지 않게
순번(seq)대로 내보낸다. 코너 콜(접착 요소)이 이전 코너의 마지막 멘트보다 먼저 나가면 안 된다.

청취자 채팅 — 도착하면 유형을 나누고 풀에 쌓아 두었다가, 인용을 허용하는 단계를 만들 때 선별기가
고른다(CORNER_DESIGN 5.1.3, 5.7절). 나머지 입력 라우팅(READ·FEATURE)은 아직 없다.
백엔드에는 v1 상태만 통보한다 — 만료·반영 확정은 계측에만 남는다.
요청별 방송 ack는 보내지 않는다 (CORNER_DESIGN D5).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..corners.run import CornerRun, MaterialSources, PlannedStep, Thread
from ..corners.steps import BLOCK_STEP, STEP_KINDS
from ..domain import (
    ABOUT_SELF,
    QUOTE_LEVEL,
    TONE_COMPLAINT,
    TONE_JOKE,
    TONE_PENDING,
    Anchor,
    BlockScript,
    ChatType,
    Decision,
    GenerationJob,
    ListenerRequest,
    Material,
    RequestState,
    ScheduleContext,
    SegmentKind,
    SegmentSubmission,
    StationConfig,
    StationProfile,
    new_id,
    wire_state,
)
from ..format.clock import Clock, ClockEntry
from ..format.spec import QUOTABLE, CornerSpec, FormatSpec, Intensity, Mode
from ..glue import WIRE_KIND, GlueBank
from ..inputs.aggregate import chat_trend
from ..inputs.callbacks import CallbackStore
from ..inputs.classify import classify, keywords, steer_rule
from ..inputs.on_air import OnAirTimeline
from ..inputs.pool import QuotePool, leans
from ..inputs.router import BOARD_STORY, CHAT, MESSAGE, SONG_REQUEST, normalize_kind
from ..inputs.rules import ChatRules
from ..inputs.selector import RELEVANCE, QuoteSelector
from ..inputs.topic_plan import NEW, Angle, TopicPlan
from ..inputs.votes import PENDING, VoteRound, view
from ..pipeline.pipeline import GenerationPipeline
from ..pipeline.tts import TtsClient
from ..sources.rss import RssCollector
from ..telemetry import Telemetry
from ..transport import Transport

_IDLE_SLEEP_SEC = 0.5
_MAX_REQUEST_FAILURES = 2  # 인용이 빠진 요청은 한 번 다시 큐에 넣고, 또 빠지면 거절한다
# 곡 소개는 버퍼가 target_buffer_sec의 이 비율로 줄었을 때 만든다. 기본 30초면 20초 —
# 세그먼트 생성 P95 약 14초(2026-11-07 시나리오 실행)에 여유를 둔 값이다 [임시값]
_SONG_INTRO_LEAD_RATIO = 2 / 3
_TTS_BLOCKED_SLEEP_SEC = 5.0
# 백엔드는 backpressure를 약 10초마다 보낸다. 세 번 연속 못 받으면 백엔드가 멈춘 것으로 보고 자체 추정으로 돌아간다
_BACKEND_REPORT_TTL_SEC = 30.0
# 클락이 코너를 연달아 이만큼 건너뛰면(전부 진입 조건 미달) 지금은 낼 것이 없다고 보고 기다린다.
# 예: 운영 중인 음악 형식 방에 아직 등록된 곡이 없다 — 곡(track.queued)이 오면 다시 진행한다
_MAX_EMPTY_CORNERS = 20
_NO_MATERIAL_SLEEP_SEC = 3.0
_WAIT = object()  # _next_planned: 낼 수 있는 단계가 지금은 없다
_PENDING = object()  # _next_planned: 지금 코너가 투표 결과를 기다린다 — 곧 다시 본다
# v1 계약은 세그먼트당 요청 하나(request_ref)만 싣는다 — 참여 강도가 2여도 1건이고, 주 소재가 있는 멘트에는
# 채팅을 함께 싣지 못한다. v2로 보낼 때(input_refs)만 여러 건을 싣는다 (#267)
_V1_QUOTES_PER_SEGMENT = 1
# 주 소재(사연·신청곡)가 있는 멘트에 함께 싣는 채팅 수 — 주 소재가 밀리지 않게 한 건만
_QUOTES_WITH_FEATURE = 1
# 한 건만 싣는 단계. 오프닝의 코너 예고는 쌓아 둔 버퍼 없이 나간다 — 두 건을 실으면 대본과 합성이
# 길어져 오프닝 도중의 무음이 늘었다 (ENGINE_TUNING_LOG T-010, O-004)
_SINGLE_QUOTE_STEPS = frozenset({"corner_preview"})
_TEXT_CORNER = "text_corner"  # 예고가 가리키는 코너
# 곡 연속 방지로 연달아 건너뛸 수 있는 곡 수 (#228). 건너뛰기는 시간이 들지 않는 결정이고, 클락이 코너를
# _MAX_EMPTY_CORNERS번 연달아 못 넣어야 기다린다. 1번이면 본 코너에 문자 소개가 있는 방송(빠짐 → 곡 → 빠짐 → 곡)에서
# 곡이 여전히 이어졌다 (2026-11-07 quiet)
_MAX_SONGS_HELD = 2
# 뉴스로 바꾼 뒤 첫 기사 수집을 기다리는 상한 — 그동안은 백엔드에 남은 분량이 나간다. 넘으면 기다리지 않고
# 편성을 잇는다(기사가 없으면 뉴스 코너는 건너뛰고, 들어오는 대로 들어간다) [임시값]
_FORMAT_READY_WAIT_SEC = 8.0
# 반응은 들은 뒤 몇 초 지나 올라온다 — 그만큼 앞에 나가던 것을 들은 것으로 본다 [임시값]
_REACTION_LAG_SEC = 5.0
_MOOD_LEVEL = 1  # 기획서 4.2절 ① — 한 건씩 인용하지 않고 분위기로 언급했다
_OPENERS = ("talk_open", "talk_resume")  # 코너의 화제를 꺼내는 단계 — 갈래와 시간 맥락은 여기에만 붙인다
# 곡 앞에서 물으려면 곡 뒤에 답을 받을 말 코너가 들어갈 시간이 남아 있어야 한다 [임시값]
_ANSWER_ROOM_SEC = 120.0
# 곡 전에 던진 질문으로 돌아오는 멘트에 주는 후보의 수 — 질문 뒤에 온 글을 이만큼 주고 답인 것만 세 건까지 고르게 한다
# (steps.ANSWER_QUOTE_RULE). 계약 v2(input_refs)로 보낼 때만 여러 건을 싣는다 [임시값]
_ANSWER_CANDIDATES = 6
_ASKED_BEFORE = 5  # 곡 소개에 알려 주는 "이미 던진 질문"의 수
# 질문을 던진 곡 소개가 제출되기를(앞 순번을) 기다리는 상한 — 넘으면 곡이 끝나는 때를 모르는 채로 진행한다
_ASK_PUBLISH_WAIT_SEC = 30.0
_QUESTION_RE = re.compile(r"[^.!?…]*[?？]")
# 차분한 말투의 DJ는 묻는 말도 마침표로 끝낸다("…느껴진 적 있으신가요.") — 물음표가 없으면 묻는 말끝으로 찾는다.
# "~세요."는 넣지 않는다. "채팅으로 남겨 주세요."가 걸린다
_ASKING_RE = re.compile(r"[^.!?…]*(?:나요|가요|까요|니까)\.")
# 시간대와, 그 시간대에 지금처럼 말하면 어긋나는 때
_DAY_PARTS = {"밤": "아침이나 낮", "새벽": "아침이나 낮, 저녁", "아침": "낮이나 저녁, 밤",
              "오전": "저녁이나 밤, 새벽", "오후": "아침이나 밤, 새벽", "저녁": "아침이나 낮, 새벽"}
_KST = timezone(timedelta(hours=9))  # 방송 시각은 한국 시간이다 (서머타임 없음)
_WEEKDAYS = "월화수목금토일"

logger = logging.getLogger(__name__)


def day_part(hour: int) -> str:
    """시간대 이름. 다섯 시는 아침이 아니라 새벽으로 부른다 — 겨울에는 해가 뜨기 전이다."""
    return ("밤" if hour in (0, 21, 22, 23) else "새벽" if hour < 6 else "아침" if hour < 9
            else "오전" if hour < 12 else "오후" if hour < 18 else "저녁")


def daytime(now: float) -> dict[str, str]:
    """모든 멘트에 알리는 시간대 — '새벽', '4시쯤'과, 지금처럼 말하면 어긋나는 때 (steps.DAYTIME_RULE)."""
    t = datetime.fromtimestamp(now, _KST)
    part = day_part(t.hour)
    return {"part": part, "clock": f"{t.hour % 12 or 12}시쯤", "others": _DAY_PARTS[part]}


def question_of(script: str) -> str:
    """대본에서 청취자에게 물은 문장만. 묻지 않았으면 빈 문자열."""
    asked = _QUESTION_RE.findall(script) or _ASKING_RE.findall(script)
    return " ".join(s.strip() for s in asked)


@dataclass
class Asked:
    """곡 앞에서 던진 질문 — 곡 뒤 첫 이야기 멘트가 받는다 (CORNER_DESIGN 7.7.2절)."""

    segment_id: str
    text: str
    at: float
    anchor_id: str | None = None  # 질문을 던진 곡 소개의 앵커 — 그 곡이 끝나는 때를 타임라인에서 찾는다
    corner: str | None = None  # 질문이 나온 이야기 코너 — 곡 뒤에 다른 코너가 올 차례면 이 코너가 먼저 답을 받는다


def time_context(now: float, started_at: float) -> str:
    """'금요일 밤 11시 40분, 방송 18분째' — 화제를 꺼낼 때 DJ가 쓸 수 있는 시간 맥락."""
    t = datetime.fromtimestamp(now, _KST)
    part = day_part(t.hour)
    clock = f"{t.hour % 12 or 12}시" + (f" {t.minute}분" if t.minute else "")
    minutes = int(max(0.0, now - started_at) // 60)
    elapsed = f"방송 {minutes}분째" if minutes else "방송 시작 직후"
    return f"{_WEEKDAYS[t.weekday()]}요일 {part} {clock}, {elapsed}"


@dataclass
class PersonaSwitch:
    """방송 중에 바꿀 페르소나 — 검사를 통과한 것만 편성 관리자에 온다 (계약 3.2절 station.persona_changed)."""

    profile: StationProfile
    tts: TtsClient  # 새 목소리
    persona_id: str | None = None
    voice: str | None = None
    snapshot: dict | None = None  # 받은 persona 원문 — 계측에 사본으로 남긴다


@dataclass
class FormatSwitch:
    """방송 중에 바꿀 형식 — 검사를 통과한 것만 편성 관리자에 온다 (계약 3.2절 station.format_changed)."""

    fmt: FormatSpec
    clock: Clock  # 오프닝 없이 순환부터 시작하는 새 형식의 클락
    intensity: Intensity
    rss: RssCollector  # 새 형식의 뉴스 소재 — 뉴스가 아니면 빈 수집기
    corners: list[str] | None = None
    news_sections: list[str] | None = None
    topic_plan: TopicPlan | None = None  # 주제 토크가 처음 생기는 전환이면 새로 만든 갈래 계획
    ready: asyncio.Event | None = None  # 첫 기사 수집이 끝났다 — 뉴스로 바꿀 때 잠깐 기다린다


class Scheduler:
    def __init__(self, *, config: StationConfig, fmt: FormatSpec, clock: Clock,
                 sources: MaterialSources, intensity: Intensity, topic: str,
                 pipeline: GenerationPipeline, glue: GlueBank, transport: Transport,
                 telemetry: Telemetry, chat_rules: ChatRules, violates,
                 target_buffer_sec: float = 30.0, max_concurrent: int = 2,
                 emit_corner_changed: bool = False, topic_plan: TopicPlan | None = None,
                 contract_version: int = 1, emit_corner_format: bool = False, tone_judge=None):
        self.config = config
        self.tone_judge = tone_judge  # 채팅 성격 판정 (inputs/chat_tone.py) — 없으면 모든 채팅을 보통으로 본다
        self._contract_version = contract_version  # 엔진→백엔드 계약 버전 (10.10절)
        self._notices: set[asyncio.Task] = set()  # 동기 콜백에서 띄운 상태 통보 — 끝날 때까지 붙잡아 둔다
        self.fmt = fmt
        self.clock = clock
        self.sources = sources
        self.intensity = intensity
        self.topic = topic
        self.topic_plan = topic_plan  # 이야기 갈래 — 없으면 주제 한 줄로 말한다
        self.pipeline = pipeline
        self.glue = glue
        self.transport = transport
        self.telemetry = telemetry
        self.target_buffer_sec = target_buffer_sec
        self.max_concurrent = max_concurrent
        self._sem = asyncio.Semaphore(max_concurrent)
        self.chat_rules = chat_rules
        self.timeline = OnAirTimeline()
        self.callbacks = CallbackStore()  # 방송에 나간 청취자 입력 — 다시 꺼내기·엔딩 돌아보기 소재
        sources.talk_points = self._talk_point
        # 일반 방의 기본 기준은 화제 관련성 우선(U12). 실험용 방은 조건이 정한다
        experiment = config.experiment or {}
        self.pool = QuotePool(
            reaction_window_sec=experiment.get("window_sec", chat_rules.reaction_window_sec),
            standalone_window_sec=chat_rules.standalone_window_sec,
            track_end=self.timeline.track_end, mood_window_sec=chat_rules.mood_window_sec,
        )
        self.selector = QuoteSelector(
            experiment.get("selector", RELEVANCE), flood_threshold=chat_rules.flood_threshold,
            max_quote_chars=chat_rules.max_quote_chars, violates=violates,
        )
        self._violates = violates
        self._aggregate_ack = experiment.get("aggregate_ack", True)  # 실험용 방의 보조 조건 (RQ-A)
        self._previewed = False  # 문자 코너까지 예고는 한 번만 — 코너마다 되풀이하지 않는다
        self._anchor_no = 0
        self._last_anchor: Anchor | None = None  # 생성 쪽 최신 앵커 — 접착 문구는 이것을 잇는다
        self._run_anchor: Anchor | None = None  # 지금 코너의 화제 앵커 — 항목 없는 단계가 처음 나올 때 만든다
        self._last_requester: str | None = None
        self._quoted: dict[str, tuple[list[ListenerRequest], GenerationJob]] = {}  # segment_id -> 반영한 입력들
        self._song_requests: dict[str, ListenerRequest] = {}  # 곡 차례를 기다리는 신청곡의 한마디
        self._generating: dict[str, ListenerRequest] = {}  # 생성 중인 입력
        self._discard: set[str] = set()  # 생성 중 취소·철회된 입력 — 결과를 버린다
        self._steer_pending = False  # 화제를 바꿀 인용을 생성 중 — 결과가 나와야 다음 단계의 화제가 정해진다
        # 곡 앞에서 묻고 곡 뒤에 답을 받는다 (CORNER_DESIGN 7.7.2절)
        self._ask_pending = False  # 질문을 던지는 곡 소개를 생성 중 — 곡 뒤 멘트를 미리 만들지 않는다
        self._asked: Asked | None = None  # 던졌고 아직 받지 않은 질문
        self._song_hold: tuple[str, float] | None = None  # 가장 최근에 소개한 곡의 (앵커, 소개를 만든 때)
        self._questions: list[str] = []  # 오늘 던진 질문들 — 같은 것을 또 묻지 않게 곡 소개에 알려 준다
        # 백엔드가 알리는 재생 시작 시각은 청취자가 듣는 시각이다(방송 지연만큼 뒤). 알림을 받은 때와의 차이가 그 지연이다
        self._air_delay_sec = 0.0
        # 프롬프트에 넣는 시각에 더하는 값. 시나리오 실행기가 방송 시각을 정할 때 쓴다(심야 방송을 낮에 시험한다)
        self.clock_offset_sec = 0.0
        self._corner_now: str | None = None  # 지금 코너와 그 앞 코너 — 이야기 코너 뒤의 곡 앞에서만 묻는다
        self._corner_before: str | None = None
        self._emit_corner_changed = emit_corner_changed
        self._emit_corner_format = emit_corner_format  # corner.changed에 형식을 싣는다 (#260)
        self._announced_corner: str | None = None
        self._inflight = 0
        # 첫 곡은 백엔드가 방송 시작과 함께 튼다 — 엔딩 시점 계산에는 넣지만 버퍼에는 넣지 않는다.
        # 그래야 오프닝을 곡이 나가는 동안 미리 만들어 둔다(첫 멘트의 생성 지연을 곡이 가려 준다)
        self._first_song_ms = int((config.first_song or {}).get("duration_ms") or 0)
        self._produced_ms = 0
        self._submitted = 0
        self._started_at: float | None = None
        self._end_at: float | None = None
        self._failures: dict[str, int] = {}  # request_id -> 생성 실패 횟수
        # 백엔드의 마지막 backpressure 보고: (D_total 초, 측정 시각, 그때까지 엔진이 제출한 ms)
        self._backend_report: tuple[float, float, int] | None = None
        self._backpressure = False
        self._run: CornerRun | None = None
        self._entered: dict[str, int] = {}  # 코너별 진입 횟수 — 두 번째부터 단계의 again을 쓴다
        # 곡 연속 방지 (#228) — 지난 곡 뒤로 다른 코너가 있었나, 그 사이 곡을 몇 번 건너뛰었나
        self._talk_since_song = True
        self._songs_held = 0
        self._skip_logged: set[tuple[str, str | None]] = set()  # 기다리는 동안 skip 로그를 반복하지 않는다
        self._last_quote_at = float("-inf")
        # 순서 보장 제출 — 순번 발급, 다음에 낼 순번, 끝났지만 앞 순번을 기다리는 결과
        self._seq = 0
        self._next_pub = 0
        self._ready: dict[int, tuple[SegmentSubmission | None, int, Anchor | None,
                                     GenerationJob | None]] = {}
        self._seq_run: dict[int, CornerRun] = {}  # 순번 → 그것을 편성한 코너 (코너 예산 — #220)
        self._deferred: PlannedStep | None = None  # 만들 때를 기다리는 곡 소개 (#239)
        # 코너 변경 (#259) — 운영자가 고른 코너, 클락 대신 들어갈 코너
        self._jump: str | None = None
        self._forced: ClockEntry | None = None
        self._persona: PersonaSwitch | None = None  # 바꿀 페르소나 (#261) — 편성 루프가 다음 차례에 바꾼다
        self._format: FormatSwitch | None = None  # 바꿀 형식 (#260)
        self.format_switches = 0  # 방송 중에 형식을 바꾼 횟수
        # 전환 뒤에 쓸 연결 멘트 — 페르소나와 형식을 잇달아 바꿔도 마지막에 받은 것이 둘 다 반영한 것이다
        self._next_glue: GlueBank | None = None
        self._jobs: dict[int, GenerationJob] = {}  # 순번 → 생성 중인 작업
        self._settling: set[int] = set()  # 생성이 끝나 결과를 정리하는 중인 순번 — 입력은 그 작업이 정리한다
        self._abandoned: set[int] = set()  # 코너 변경으로 버린 순번 중 생성이 아직 끝나지 않은 것
        # 제출했지만 재생 시작을 확인하지 못한 세그먼트 — 백엔드가 버리면 곡·입력을 되돌린다
        self._unaired: dict[str, tuple[SegmentSubmission, GenerationJob | None, int]] = {}
        self._announced_segment: str | None = None
        self._pub_lock = asyncio.Lock()
        # 청취자가 적은 방송의 흐름 (CORNER_DESIGN 7.7.1절, #262)
        self._last_chat_at = float("-inf")  # 마지막 채팅 도착 — 한동안 없으면 조용한 방송이다
        self._last_question_at = float("-inf")  # 청취자에게 마지막으로 물은 때
        self._last_callback_at = float("-inf")  # 청취자 이야기를 마지막으로 다시 꺼낸 때
        self._last_mood_at = float("-inf")  # 반응 분위기를 마지막으로 전한 때
        self._last_talk_corner: str | None = None  # 곡 소개가 아닌 마지막 코너
        self._song_since_talk = False  # 그 코너 뒤에 곡이 나갔다
        self._last_track: dict | None = None  # 마지막으로 소개한 곡 — 곡 뒤 첫 멘트가 받는다
        self._song_returns = 0  # 곡 뒤에 말을 이은 횟수 — 곡을 받는 말은 한 번 걸러 한 번만 한다
        self._quiet_visits = 0  # 조용한 동안 한 번에 쓴 진입 수 — 몇 번에 한 번은 곡 이야기로 쉬어 간다

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def pending_count(self) -> int:
        return len(self.pool)

    @property
    def current_corner(self) -> str | None:
        return self._run.spec.corner_type if self._run else None

    async def accept_request(self, req: ListenerRequest) -> None:
        """입력을 종류별 저장소로 보낸다 (CORNER_DESIGN 4.4절)."""
        req.kind = normalize_kind(req.kind)
        if not self.fmt.accepts(req.kind):
            self.telemetry.log_input(req, None, "not_on_air")
            await self._transition(req, RequestState.NOT_ON_AIR)
        elif req.kind == MESSAGE:
            self.telemetry.log_input(req, None, "inbox")
            self.sources.inbox.add(req)
            await self._transition(req, RequestState.INBOXED)
        elif req.kind == SONG_REQUEST:
            self.telemetry.log_input(req, None, "song_request")
            self._song_requests[req.request_id] = req
            await self._transition(req, RequestState.INBOXED)
        else:
            self.enqueue_request(req)
            # v1은 직전의 screened가 "받았다"를 알렸다. v2에는 screened가 없어 후보 풀에 들어간 것을 알린다 (10.4절)
            wire = wire_state(req.state, self._contract_version)
            if self._contract_version >= 2 and wire is not None:
                await self.transport.notify_request_state(req.request_id, wire)

    def attach_song_request(self, track: dict, request_id: str) -> None:
        """track.queued의 곡이 신청곡이면 한마디와 닉네임을 곡 소개 소재에 붙인다 (계약 3.4절)."""
        req = self._song_requests.get(request_id)
        if req is None:
            return
        track["request_id"] = request_id
        if req.body:
            track["requester_comment"] = req.body
        if req.nickname:
            track.setdefault("requester_nickname", req.nickname)

    def add_story(self, story: ListenerRequest) -> None:
        if self.sources.stories.add(story):
            self.telemetry.log_input(story, None, "story_pool")

    def withdraw_story(self, story_id: str) -> None:
        """다른 방에서 읽힌 사연. 이미 생성 중이면 결과를 버린다 (계약 3.5절)."""
        if not self.sources.stories.withdraw(story_id) and story_id in self._generating:
            self._discard.add(story_id)

    async def cancel_request(self, request_id: str) -> None:
        """대기 중이면 빼고, 생성 중이면 끝까지 만들고 버린다. 제출 후는 백엔드 몫이다 (계약 3.2절)."""
        req = (self.pool.pop(request_id) or self.sources.inbox.remove(request_id)
               or self._song_requests.pop(request_id, None))
        if req is not None:
            await self._transition(req, RequestState.CANCELLED)
        elif request_id in self._generating:
            self._discard.add(request_id)

    def skip_to(self, corner_type: str) -> None:
        """운영자가 고른 코너로 넘어간다 (계약 3.2절 corner.skip). 편성 루프가 다음 차례에 지금 코너를 끝낸다."""
        self._jump = corner_type

    def switch_persona(self, switch: PersonaSwitch) -> None:
        """운영자가 고른 페르소나로 바꾼다 (계약 3.2절 station.persona_changed). 편성 루프가 다음 차례에 바꾼다."""
        self._persona = switch

    @property
    def pending_persona_id(self) -> str | None:
        return self._persona.persona_id if self._persona else None

    def switch_format(self, switch: FormatSwitch) -> None:
        """운영자가 고른 형식으로 바꾼다 (계약 3.2절 station.format_changed). 편성 루프가 다음 차례에 바꾼다."""
        self._format = switch

    @property
    def upcoming_format(self) -> tuple[str, list[str] | None, list[str] | None]:
        """바꾸는 중인 것까지 넣은 형식과 호스트 선택 — 재전달을 가린다."""
        if self._format is not None:
            return self._format.fmt.format, self._format.corners, self._format.news_sections
        return self.config.format, self.config.corners, self.config.news_sections

    def use_glue(self, glue: GlueBank) -> None:
        """다음 전환부터 쓸 연결 멘트 — 합성이 끝나는 대로 채워진다."""
        self._next_glue = glue

    def _take_glue(self) -> None:
        if self._next_glue is not None:
            self.glue, self._next_glue = self._next_glue, None

    async def discard_segments(self, segment_ids: list[str]) -> None:
        """백엔드가 재생하지 않고 버린 세그먼트 (계약 3.2절 segment.discarded).

        소개하려던 곡과 인용한 입력을 되돌리고, 그 분량을 버퍼 계산과 송출 위치 추정에서 뺀다.
        모르는 세그먼트와 이미 재생을 시작한 세그먼트는 무시한다.
        """
        dropped = returned = 0
        for segment_id in segment_ids:
            entry = self._unaired.get(segment_id)
            if entry is None:
                continue
            sub, job, track_ms = entry
            dropped += 1
            self._produced_ms -= sub.duration_ms + track_ms
            self.timeline.remove(segment_id)
            if segment_id == self._announced_segment:  # 화면을 바꿀 세그먼트가 사라졌다 — 다음 세그먼트가 다시 알린다
                self._announced_corner = None
            returned += await self._take_back(sub, job)
        if dropped:
            self.telemetry.log_discarded(dropped, returned)

    def on_segment_published(self, segment_id: str, at: float) -> None:
        """PUBLISHED — 대본을 송출 이력으로 옮긴다 (context-memory 3.1절). 재전달은 무시한다."""
        self._unaired.pop(segment_id, None)
        script = self.pipeline.take_aired(segment_id)
        if script is not None:
            self.telemetry.log_aired(segment_id, at, script)

    def enqueue_request(self, req: ListenerRequest) -> None:
        anchor = self.timeline.current(req.received_at)
        req.chat_type, req.rule = classify(req.body, anchor, self.chat_rules)
        req.heard = self.timeline.heard(req.received_at - _REACTION_LAG_SEC)
        req.back_ref = req.rule == "pointing" or any(p in req.body for p in self.chat_rules.steer_clarify)
        if anchor is not None:
            req.anchor_id, req.anchor_seq = anchor.id, anchor.seq
        self.telemetry.log_input(req, anchor.kind if anchor else None)
        req.state = RequestState.QUEUED
        self.telemetry.log_request_state(req.request_id, req.state)
        self.pool.add(req)
        self._last_chat_at = max(self._last_chat_at, req.received_at)
        if self.tone_judge is not None and req.kind == CHAT:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # 루프 밖에서 부른 동기 호출 — 판정하지 않고 보통 채팅으로 둔다
                return
            req.tone = TONE_PENDING
            task = loop.create_task(self._judge_tone(req))
            self._notices.add(task)
            task.add_done_callback(self._notices.discard)

    async def _judge_tone(self, req: ListenerRequest) -> None:
        """채팅의 성격을 묻는다. 불만이면 후보에서 뺀다 — 인용도 분위기 전달도 하지 않는다 (CORNER_DESIGN 5.1.4절)."""
        try:
            req.tone = await self.tone_judge.judge(req)
        except Exception:  # 판정이 채팅을 붙잡아 두면 안 된다
            logger.exception("CHAT_TONE_FAILED %s", req.request_id)
            req.tone = None
        if req.tone == TONE_COMPLAINT and self.pool.pop(req.request_id) is not None:
            await self._transition(req, RequestState.EXPIRED)
        # 혼자 뜻이 통하는 글인지는 LLM의 판정이 규칙 분류보다 앞선다 (CORNER_DESIGN 5.1.5절)
        if req.about == ABOUT_SELF:
            req.chat_type = ChatType.STANDALONE
        elif req.about is not None:
            req.chat_type = ChatType.REACTION

    def on_track_started(self, track_ref: str, started_at: float) -> None:
        self._air_delay_sec = max(0.0, started_at - time.time())
        self.timeline.sync_track(track_ref, started_at)

    def on_segment_started(self, segment_id: str, started_at: float) -> None:
        """재생 시작 확인 — 인용이 들어 있으면 반영 확정 (CORNER_DESIGN 9.3, 9.4절). 재전달은 무시한다."""
        self._unaired.pop(segment_id, None)
        self._air_delay_sec = max(0.0, started_at - time.time())
        self.sources.votes.started(segment_id, started_at)  # 투표를 여는 멘트면 창이 이때 열린다
        if not self.timeline.sync_started(segment_id, started_at):
            return
        quoted = self._quoted.pop(segment_id, None)
        if quoted is None:
            return
        reqs, job = quoted
        for req in reqs:
            self._reflect(req, job, segment_id, started_at)

    def _reflect(self, req: ListenerRequest, job: GenerationJob, segment_id: str, started_at: float) -> None:
        req.state = RequestState.REFLECTED
        self.telemetry.log_request_state(req.request_id, req.state)
        self._notify_reflected(req, segment_id, started_at)
        if req.tone != TONE_JOKE:  # 농담은 그 자리에서 끝난 말이다 — 엔딩에서 돌아보면 진지한 말이 된다
            self.callbacks.add(req, started_at)
        # 곡 재생 중에 도착한 반응은 곡 위에서 말할 수 없어 기다린다 — 그 구조적 대기를 뺀다
        song_end = self.timeline.track_end(req.anchor_id)
        waited = max(0.0, song_end - req.received_at) if song_end else 0.0
        topic_match = None
        if req.chat_type == ChatType.REACTION and req.anchor_seq is not None and job.anchor is not None:
            topic_match = job.anchor.seq - req.anchor_seq <= 1
        self.telemetry.log_reflection(
            req=req, segment_id=segment_id, started_at=started_at,
            quote_age_net=started_at - req.received_at - waited,
            topic_match=topic_match, script_chars=job.script_chars,
        )

    def _notify_reflected(self, req: ListenerRequest, segment_id: str, started_at: float) -> None:
        """계약 v2 — 입력이 든 세그먼트가 재생되기 시작했다고 백엔드에 알린다 (10.4절). v1에는 이 값이 없다."""
        wire = wire_state(RequestState.REFLECTED, self._contract_version)
        if wire is None or req.kind == BOARD_STORY:  # 게시판 사연은 요청이 아니다 (계약 3.5절)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 루프 밖에서 부른 동기 호출 — 알릴 곳이 없다
            return
        task = loop.create_task(self.transport.notify_request_state(
            req.request_id, wire, {"segment_id": segment_id, "started_at": started_at}))
        self._notices.add(task)
        task.add_done_callback(self._notices.discard)

    def report_backpressure(self, d_total_ms: float, severity: str, at: float | None = None) -> None:
        """백엔드가 잰 실제 남은 방송 분량(계약 3.2절 backpressure).

        백엔드 송출이 실시간보다 느려지면(멘트 사이 폴백 등) 엔진 자체 추정은 큐가 쌓이는 것을
        모른다 — 그대로 두면 큐가 시간에 비례해 불어나 요청 반영이 몇 분씩 밀린다.
        """
        now = time.time()
        measured_at = min(at, now) if at else now  # 시계가 같은 머신 전제. 미래 시각은 지금으로
        self._backend_report = (max(0.0, d_total_ms / 1000), measured_at, self._produced_ms)
        self._backpressure = severity in ("low", "critical")

    def _buffer_sec(self, now: float) -> tuple[float, str]:
        if self._backend_report is not None:
            d_total, measured_at, produced_then = self._backend_report
            if now - measured_at <= _BACKEND_REPORT_TTL_SEC:
                # 보고 이후 흐른 시간만큼 줄고, 보고 이후 엔진이 낸 분량만큼 는다
                buffer = d_total - (now - measured_at) + (self._produced_ms - produced_then) / 1000
                return max(0.0, buffer), "backend"
        # 보고가 없거나 오래됐으면(백엔드 미연결·로컬 실행) 제출한 오디오 총 길이 - 경과 시간
        elapsed = now - (self._started_at or now)
        return max(0.0, self._produced_ms / 1000 - elapsed), "engine"

    def _remaining_sec(self, now: float) -> float:
        """지금까지 낸 오디오가 끝나는 시각부터 방송 종료까지 — 클락이 엔딩 시점을 정하는 기준."""
        if self._started_at is None or self._end_at is None:
            return float(self.config.broadcast_minutes * 60)
        aired_until = max(now, self._started_at
                          + (self._first_song_ms + self._produced_ms) / 1000)
        return self._end_at - aired_until

    def _snapshot(self) -> ScheduleContext:
        now = time.time()
        buffer_sec, source = self._buffer_sec(now)
        return ScheduleContext(
            now=now,
            pending_requests=list(self.pool.items),
            generated_buffer_sec=buffer_sec,
            inflight_generations=self._inflight,
            backpressure=self._backpressure,
            corner=self.current_corner,
            step_index=self._run.step_index if self._run else 0,
            remaining_sec=self._remaining_sec(now),
            buffer_source=source,
        )

    async def run(self, stop: asyncio.Event, max_segments: int | None = None) -> None:
        """max_segments는 관통 테스트용 — N개를 편성한 뒤 종료한다."""
        self._started_at = time.time()
        self._end_at = self._started_at + self.config.broadcast_minutes * 60
        if self._first_song_ms and self.config.first_song.get("track_ref"):
            song = self.config.first_song
            self._last_anchor = self.timeline.new_anchor(
                self._next_anchor_id("track"), "track",
                keywords(f"{song.get('title', '')} {song.get('artist', '')}"))
            self.timeline.append(None, self._last_anchor, 0, self._started_at,
                                 track_ref=song["track_ref"], track_ms=self._first_song_ms)
        while not stop.is_set() and time.time() < self._end_at:
            if max_segments is not None and self._seq >= max_segments:
                break
            await self._expire_inputs(time.time())
            if self._persona is not None:
                await self._apply_persona()
            if self._format is not None:
                await self._apply_format()
            if self._jump is not None:
                await self._cut_corner()
            ctx = self._snapshot()
            blocked_sec = self.pipeline.tts_blocked_until - ctx.now
            if blocked_sec > 0:  # 합성할 수 없으니 LLM도 부르지 않는다. 입력은 창이 지나면 만료된다
                self.telemetry.log_decision(ctx, Decision(action="tts_blocked"))
                await asyncio.sleep(min(blocked_sec, _TTS_BLOCKED_SLEEP_SEC))
                continue
            if (ctx.inflight_generations >= self.max_concurrent
                    or ctx.generated_buffer_sec >= self.target_buffer_sec
                    or self._steer_pending or self._ask_pending or self._waits_for_answers(ctx.now)
                    or self._waits_for_song(ctx.now)):
                self.telemetry.log_decision(ctx, Decision(action="wait"))
                await asyncio.sleep(_IDLE_SLEEP_SEC)
                continue
            # 투표 창이 열린 동안의 이야기는 그 멘트가 방송에 나갈 시각으로 창을 본다 (#279)
            self.sources.votes.queued_until = ctx.now + ctx.generated_buffer_sec
            planned = self._deferred or self._next_planned(ctx)
            if planned is None:  # 엔딩까지 편성을 마쳤다 — 남은 생성이 끝나면 종료
                self.telemetry.log_decision(ctx, Decision(action="idle"))
                break
            if planned is _PENDING:  # 투표 창이 닫히길 기다린다 (#279) — 그동안 만든 멘트가 방송을 잇는다
                self.telemetry.log_decision(ctx, Decision(action="wait", corner_type=self.current_corner,
                                                          step="game_reveal"))
                await asyncio.sleep(_IDLE_SLEEP_SEC)
                continue
            if planned is _WAIT:  # 진입할 수 있는 코너가 없다 — 소재(곡 등)가 들어오길 기다린다
                self.telemetry.log_decision(ctx, Decision(action="no_material"))
                await asyncio.sleep(_NO_MATERIAL_SLEEP_SEC)
                continue
            if self._too_early_for_song_intro(planned, ctx):
                self._deferred = planned
                self.telemetry.log_decision(ctx, Decision(
                    action="wait", corner_type=planned.corner.corner_type, step=planned.step.kind))
                await asyncio.sleep(_IDLE_SLEEP_SEC)
                continue
            if self._deferred is not None:
                self._deferred = None
                self._refresh_track(planned)
            if planned.step.kind == "glue":
                await self._publish_glue(ctx, planned)
            else:
                job = await self._build_job(planned)
                selection = job.material.extra.pop("selection", None)
                self.telemetry.log_decision(ctx, Decision(
                    action="generate", corner_type=job.corner_type, step=job.step,
                    quotes=[q.request_id for q in job.material.quotes],
                    situation=selection.situation if selection else None,
                    criterion=selection.criterion if selection else None,
                    material=job.material.extra.get("material")))
                seq = self._reserve()
                self._jobs[seq] = job
                asyncio.create_task(self._generate(job, seq))
            await asyncio.sleep(0)  # 생성 태스크에 실행 양보
        while self._inflight > 0 or self._abandoned:  # 진행 중 생성 마무리 대기 (버린 생성도 계측을 쓴다)
            await asyncio.sleep(0.1)
        if max_segments is None:  # 방송 종료 — 남은 입력은 반영되지 못했다 (관통 테스트는 중간에 끊을 뿐이다)
            leftovers = [*self.pool.drain(), *self.sources.inbox.drain(), *self._song_requests.values()]
            self._song_requests.clear()
            await self._mark_expired(leftovers)

    # ── 클락 · 코너 ──────────────────────────────────────────────────

    def _too_early_for_song_intro(self, planned: PlannedStep, ctx: ScheduleContext) -> bool:
        """곡 소개는 곡 앞의 마지막 인용 기회다 — 앞 멘트가 충분히 남았으면 아직 만들지 않는다 (#239).

        미리 만들면 그 뒤에 온 채팅은 곡이 끝날 때까지(약 3.5분) 나갈 수 없다.
        생성 중인 앞 멘트는 버퍼에 아직 안 잡히므로, 그때는 더 적게 남았을 때만 만든다.
        """
        if planned.step.kind != "music_intro":
            return False
        lead = self.target_buffer_sec * _SONG_INTRO_LEAD_RATIO
        if ctx.inflight_generations:
            lead /= 2
        return ctx.generated_buffer_sec > lead

    def _refresh_track(self, planned: PlannedStep) -> None:
        """기다리는 동안 새 곡(신청곡)이 알려졌으면 그 곡을 소개한다 — 백엔드는 새 곡을 튼다."""
        self.sources.tracks.put_back(planned.items[0])
        if (track := self.sources.tracks.take()) is not None:
            planned.items[0] = track

    def _next_planned(self, ctx: ScheduleContext):
        """다음 단계. 엔딩까지 끝났으면 None, 지금은 진입할 코너가 없으면 _WAIT."""
        empty = 0
        while True:
            if self._run is None:
                if self._forced is None:
                    self._forced = self._answer_corner()
                if self._forced is not None:  # 운영자가 고른 코너 — 못 들어가면 원래 순환으로 이어진다
                    entry, self._forced = self._admit(self._forced, forced=True), None
                else:
                    entry = self.clock.next(ctx.remaining_sec)
                    if entry is None:
                        return None
                    entry = self._admit(entry)
                if entry is None:
                    empty += 1
                    if empty >= _MAX_EMPTY_CORNERS:
                        return _WAIT
                    continue
                self._skip_logged.clear()
                self._note_entry(entry.corner)
                self.sources.inbox.begin_corner()
                if entry.corner.corner_type == _TEXT_CORNER:
                    self._previewed = False
                topic = entry.label or self.topic
                self._run_anchor = None
                corner_type = entry.corner.corner_type
                plays_song = self._plays_song(entry.corner)
                self._run = CornerRun(entry, sources=self.sources, intensity=self.intensity,
                                      thread=Thread(topic=topic, keywords=sorted(keywords(topic))),
                                      again=self._entered.get(corner_type, 0) > 0,
                                      continues=not plays_song and self._last_talk_corner == corner_type,
                                      after_song=not plays_song and self._song_since_talk)
                self._entered[corner_type] = self._entered.get(corner_type, 0) + 1
                self._corner_before, self._corner_now = self._corner_now, corner_type
                if plays_song:
                    self._song_since_talk = True
                else:
                    self._last_talk_corner, self._song_since_talk = corner_type, False
                self.telemetry.log_corner(entry.corner.corner_type, entry.label, "enter",
                                          self._produced_ms, entry.corner.title)
            planned = self._run.next_step()
            if planned is PENDING:
                return _PENDING
            if planned is not None:
                if planned.step.kind == "glue" and self.glue.get(
                        planned.corner.corner_type, planned.step.glue) is None:
                    continue  # 이 코너는 그 문구가 없다
                if planned.step.glue == "resume" and self._run.continues:
                    continue  # 곡만 사이에 두고 같은 코너를 잇는다 — 돌아왔다고 말하지 않는다
                return planned
            self.telemetry.log_corner(self._run.spec.corner_type, self._run.label, "exit",
                                      self._produced_ms, self._run.spec.title)
            self._run.closed = True
            if self._run.pending == 0:  # 생성 중인 것이 남았으면 마지막 것이 끝날 때 기록한다
                self._log_corner_length(self._run)
            self._run = None
            empty += 1
            if empty >= _MAX_EMPTY_CORNERS:
                return _WAIT

    def _admit(self, entry: ClockEntry, *, forced: bool = False) -> ClockEntry | None:
        """진입 조건(requires)을 못 채우면 대체 코너로, 대체도 없으면 이번 차례를 건너뛴다.

        forced는 운영자가 고른 코너다 — 곡 연속 방지로 미루지 않는다.
        """
        if not forced and self._holds_song(entry.corner):
            self._songs_held += 1
            self.telemetry.log_corner(entry.corner.corner_type, entry.label, "hold", self._produced_ms,
                                      entry.corner.title)
            return None
        if self.sources.satisfies(entry.corner, entry.label):
            return entry
        fallback = entry.corner.fallback
        if fallback and self.sources.satisfies(self.fmt.corner(fallback), entry.label):
            self.telemetry.log_corner(entry.corner.corner_type, entry.label, "fallback",
                                      self._produced_ms, entry.corner.title)
            return ClockEntry(self.fmt.corner(fallback), entry.label)
        key = (entry.corner.corner_type, entry.label)
        if key not in self._skip_logged:  # 소재를 기다리는 동안 같은 skip을 계속 쌓지 않는다
            self._skip_logged.add(key)
            self.telemetry.log_corner(*key, "skip", self._produced_ms, entry.corner.title)
        return None

    async def _cut_corner(self) -> None:
        """지금 코너를 끝내고 운영자가 고른 코너를 다음 차례로 잡는다 (#259)."""
        corner_type, self._jump = self._jump, None
        entry = self.clock.jump(corner_type)
        if entry is None:
            self.telemetry.log_corner_request(
                corner_type, "지금은 바꿀 수 없는 코너" if corner_type in self.fmt.corners
                else "이 형식에 없는 코너")
            return
        self.telemetry.log_corner_request(corner_type)
        await self._drop_unsubmitted()
        self._forced = entry

    async def _apply_persona(self) -> None:
        """새 페르소나로 바꾼다 (#261) — 이 뒤로 만드는 대본과 목소리가 새 DJ의 것이다.

        아직 제출하지 않은 멘트는 이전 DJ의 대본이라 내보내지 않고 곡과 입력을 되돌린다. 이미 제출한 것은
        백엔드가 버리고 segment.discarded로 알린다. 하던 말 코너는 새 DJ가 처음부터 다시 한다 — 이전 DJ가
        쓰던 이야기 덩어리를 새 목소리로 잇지 않는다. 곡 소개는 다시 하지 않는다(곡은 다음 곡 차례에 나온다) —
        새 DJ가 말부터 하게 한다.
        """
        switch, self._persona = self._persona, None
        run = self._run
        dropped = await self._drop_unsubmitted()
        before = self.config.profile.dj_name
        self.config.profile, self.config.persona_id = switch.profile, switch.persona_id
        self.config.voice = switch.voice
        self.pipeline.switch_persona(switch.profile, switch.tts)
        self._take_glue()
        # 새 DJ는 이전 DJ가 방금 누구를 인용했는지와 상관없이 바로 인용한다
        self._last_quote_at = float("-inf")
        self._last_requester = None
        self.telemetry.log_persona_switch(before, switch.profile.dj_name, switch.snapshot, dropped)
        if run is not None and self._forced is None and not self._plays_song(run.spec):
            self._forced = ClockEntry(run.spec, run.label)

    async def _apply_format(self) -> None:
        """새 형식으로 바꾼다 (#260) — 오프닝을 다시 하지 않고 새 형식의 순환으로 들어간다.

        아직 제출하지 않은 멘트는 이전 형식의 것이라 내보내지 않는다. 페르소나·주제·남은 방송 시간·방송된
        청취자 이야기는 그대로다. 쌓여 있던 입력도 그대로 둔다 — 문자·사연·신청곡은 그것을 읽는 형식으로
        돌아오면 쓰이고, 채팅은 인용 창이 지나면 만료된다. 새로 오는 입력은 새 형식의 계약으로 받는다.
        """
        switch, self._format = self._format, None
        dropped = await self._drop_unsubmitted()
        before = self.fmt
        self.fmt, self.clock, self.intensity = switch.fmt, switch.clock, switch.intensity
        self.config.format, self.config.corners = switch.fmt.format, switch.corners
        self.config.news_sections = switch.news_sections
        self.sources.use_rss(switch.rss)
        if switch.topic_plan is not None:
            self.topic_plan = switch.topic_plan
        self._take_glue()
        self.telemetry.corner_titles.update({name: spec.title for name, spec in switch.fmt.corners.items()})
        self._jump = None
        # 형식에 전환용 첫 코너가 있으면 거기부터 — 음악은 곡보다 DJ의 이야기를 먼저 들려준다
        entry = switch.fmt.entry_corner
        self._forced = ClockEntry(switch.fmt.corner(entry)) if entry else None
        # 새 형식의 흐름은 처음부터 센다 — 앞 형식의 곡·말 코너 기억으로 코너를 미루거나 문구를 빼지 않는다
        self._last_talk_corner, self._song_since_talk = None, False
        self._talk_since_song, self._songs_held = True, 0
        self._previewed = False
        self._skip_logged.clear()
        self._announced_corner = None  # 같은 이름의 코너여도 새 형식의 계약으로 다시 알린다
        self.format_switches += 1
        self.telemetry.log_format_switch(before.label, switch.fmt.label, dropped)
        if switch.ready is not None and not switch.ready.is_set():
            try:
                await asyncio.wait_for(switch.ready.wait(), _FORMAT_READY_WAIT_SEC)
            except TimeoutError:
                logger.warning("FORMAT_SOURCE_SLOW %s 기사 수집을 기다리지 않고 편성을 잇는다",
                               self.config.station_id)

    async def _drop_unsubmitted(self) -> int:
        """아직 제출하지 않은 멘트를 버리고 지금 코너를 끝낸다 (#259). 버린 수를 돌려준다.

        생성 중이거나 앞 순번을 기다리는 멘트는 내보내지 않고 곡과 입력을 되돌린다.
        이미 제출한 것 중 재생 전인 멘트는 백엔드가 버리고 segment.discarded로 알린다.
        버린 생성은 끝까지 돌게 두되 다음 코너가 그 자리를 기다리지 않게 동시 생성 한도를 새로 연다.
        """
        dropped = self._seq - self._next_pub
        async with self._pub_lock:
            for seq in range(self._next_pub, self._seq):
                if seq in self._ready:  # 생성은 끝났고 앞 순번을 기다리던 것
                    sub, _, _, job = self._ready.pop(seq)
                    if sub is not None:
                        await self._take_back(sub, job)
                    continue
                self._abandoned.add(seq)
                self._inflight -= 1
                self._spend(seq, None)
                job = self._jobs.get(seq)
                if job is None or seq in self._settling:
                    continue
                self._put_back_track(job)
                for req in _inputs(job):
                    self._generating.pop(req.request_id, None)
                    await self._return_input(req)
            self._next_pub = self._seq
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._steer_pending = False
        self._ask_pending = False
        self.sources.votes.abandon()  # 진행 중인 투표 라운드를 접는다 — 열린 투표의 결과는 받지 않는다
        if self._deferred is not None:  # 만들 때를 기다리던 곡 소개 — 곡을 되돌린다
            self.sources.tracks.put_back(self._deferred.items[0])
            self._deferred = None
            dropped += 1
        if self._run is not None:
            self.telemetry.log_corner(self._run.spec.corner_type, self._run.label, "exit",
                                      self._produced_ms, self._run.spec.title)
            self._run.closed = True
            if self._run.pending == 0:
                self._log_corner_length(self._run)
            self._run = None
        return dropped

    def _plays_song(self, corner: CornerSpec) -> bool:
        """곡을 소개하는(틀게 하는) 코너. 엔딩은 마지막 곡이 있어도 건너뛰지 않는다."""
        return corner.corner_type != self.fmt.closing and any(s.items == "track" for s in corner.steps)

    def _holds_song(self, corner: CornerSpec) -> bool:
        """곡 연속 방지 (#228) — 지난 곡 뒤로 곡 소개 말고 진입한 코너가 없으면 이번 곡은 건너뛴다.

        연달아서는 _MAX_SONGS_HELD번까지만 건너뛴다. 사이 코너가 계속 빠지면(본 코너가 문자 소개뿐이고 문자가
        없는 방송) 곡까지 막혀 무음이 된다. 신청곡은 기다리게 하지 않는다.
        """
        if (self.fmt.songs_back_to_back or self._talk_since_song
                or self._songs_held >= _MAX_SONGS_HELD):
            return False
        if not self._plays_song(corner):
            return False
        track = self.sources.tracks.peek()
        return not (track and (track.get("requested") or track.get("request_id")))

    def _note_entry(self, corner: CornerSpec) -> None:
        if self._plays_song(corner):
            self._talk_since_song = False
            self._songs_held = 0
        else:
            self._talk_since_song = True

    async def _build_job(self, planned: PlannedStep) -> GenerationJob:
        corner, step = planned.corner, planned.step
        material = Material(items=list(planned.items), label=planned.label,
                            topic=self._run.thread.topic if self._run else self.topic)
        material.extra["daytime"] = daytime(time.time() + self.clock_offset_sec)
        if self._run is not None and self._run.thread.steered:
            material.extra["steered"] = True
        feature = None
        step_kind = step.kind  # 조용한 방송에서는 꺼내는 단계가 이야기 덩어리로 바뀐다
        block: BlockScript | None = None
        if step.items == "track":
            material.track = planned.items[0]
            material.items = []
            self._last_track = material.track
            feature = self._song_requests.pop(material.track.get("request_id"), None)
        elif step.items in ("message", "board_story"):
            feature = planned.items[0]["request"]
            if step.items == "message":
                material.must_read = feature.body
        if feature is not None:
            material.feature_ref = feature.request_id
        if step.kind == "opening_greeting" and self.config.first_song:
            material.extra["first_song"] = self.config.first_song
        elif step.kind == "corner_preview":
            material.extra.update(guide=self.fmt.guide, corners=self.clock.main_titles())
        elif step.kind in _OPENERS:
            block = self._plan_block(corner)
            self._attach_asked(material)
            self._open_thread(material, block)
            if block is not None:
                step_kind = BLOCK_STEP
        elif step.kind == "closing_lookback":
            material.extra["lookback"] = self.callbacks.lookback()
            if self.topic_plan is not None and self.topic_plan.used:
                material.extra["covered_angles"] = [a.title for a in self.topic_plan.used]
        elif step.kind == "news_recap":
            material.extra["covered"] = list(self.sources.covered_news)
            self.sources.covered_news.clear()  # 다음 순환은 새로 센다
        elif step.kind == "game_final":
            material.extra["score"] = dict(self.sources.votes.score)
        if step.items in ("vote", "vote_wait", "vote_result"):
            round_: VoteRound = planned.items[0]["round"]
            if step.items == "vote_result":  # 결과를 공개한다 — 점수는 이 멘트를 만들기 전에 매긴다
                self.sources.votes.settle()
                self.telemetry.log_vote_result(round_, self.sources.votes.score)
            material.items = [view(round_, self.sources.votes.score)]
            material.extra["round"] = round_
        point = planned.items[0] if step.items == "talk_point" else None
        if point is not None:
            material.items = []
        anchor = self._anchor_for(planned)
        if step.items == "track":
            # 반응은 곡 하나만 넘긴다. 곡 뒤에 이야기 없이 또 곡이 오면(신청곡 등), 앞 곡 전에 온 반응은 두 곡 뒤의
            # 멘트가 전하게 된다 — "앞선 곡과 신청곡을 건너뛰며 지나간 화제를 불쑥 꺼낸다" (시험 실행 20261119-211622)
            if self._song_hold is not None:
                await self._mark_expired(self.pool.drop_moods(before=self._song_hold[1]))
            self._song_hold = (anchor.id, time.time())
        await self._expire_inputs(time.time())
        if point is not None and point["source"] == "block":  # 이미 쓴 덩어리의 다음 문단 — 합성만 한다
            material.extra["material"] = "block"
            return GenerationJob(
                job_id=new_id("job"), corner_type=corner.corner_type, step=BLOCK_STEP, material=material,
                plain=corner.subjectivity == "none", anchor=anchor, block=point["block"], part=point["part"],
            )
        if step_kind == "talk":
            self._attach_asked(material)
        material.quotes = self._take_quotes(corner.mode("chat"), step_kind, anchor, material)
        # 반응을 뭉쳐서 전하는 자리는 곡 뒤 첫 멘트 하나다 — 이야기를 새로 여는 자리라 끊을 흐름이 없다.
        # 그 멘트가 질문의 답을 받는 차례면 답만 받는다. 이 자리를 놓친 반응은 받지 않는다 (5.1.5절, T-017)
        if material.extra.get("after_song"):
            if not material.extra.get("asked"):
                self._take_mood(corner.mode("chat"), step_kind, material)
            await self._mark_expired(self.pool.drop_moods())
        if step_kind == "talk" and material.quotes and self._run is not None and self._run.asked:
            material.extra.setdefault("asked_earlier", self._run.asked)
        # 채팅 한 건을 받는 곡 소개에서는 받고 나서 묻는다. 여러 건이나 반응 분위기까지 받으면 멘트가 너무 길다
        if (step_kind == "music_intro" and len(material.quotes) <= 1 and not material.mood
                and self._should_ask(material, time.time())):
            material.extra["ask"] = True
            material.extra["ask_corner"] = self._corner_before
            material.extra["asked_before"] = self._questions[-_ASKED_BEFORE:]
            self._ask_pending = True
            self._last_question_at = time.time()
        if point is not None:
            if point["source"] == "chat" and not material.quotes:  # 선별기가 고르지 않았다 — 다른 소재로
                point = self._quiet_point(self._run) or {"source": "topic"}
            self._use_point(point, material)
        # 답을 받는 멘트는 화제를 바꾸지 않는다 — 후보 가운데 어느 글을 받을지는 대본이 나와야 안다
        if corner.mode("chat") == Mode.STEER and not material.extra.get("optional_quotes"):
            self._mark_steer(material)
        self._mark_aggregate(corner.mode("chat"), step_kind, material)
        if block is not None:
            material.extra["material"] = "block"
        return GenerationJob(
            job_id=new_id("job"), corner_type=corner.corner_type, step=step_kind,
            material=material, plain=corner.subjectivity == "none", anchor=anchor, feature=feature,
            block=block,
        )

    def _is_quiet(self, now: float) -> bool:
        """채팅 후보가 없고 마지막 채팅 뒤로 한동안 지났다 — DJ가 끌고 간다 (CORNER_DESIGN 7.7.1절)."""
        return not len(self.pool) and now - self._last_chat_at >= self.chat_rules.quiet_after_sec

    def _plan_block(self, corner: CornerSpec) -> BlockScript | None:
        """조용한 방송이면 이번 진입을 한 번에 쓴다. 채팅이 오가는 동안은 짧은 멘트로 빨리 반영한다."""
        parts = self.chat_rules.block_parts
        if (parts < 2 or self._run is None or not self._is_quiet(time.time())
                or not any(step.items == "talk_point" for step in corner.steps)):
            return None
        self._run.block = BlockScript(parts)
        self._run.block_next = 1
        # 덩어리가 이번 진입의 말을 다 쓴다 — 문단 뒤에 주제만으로 한 줄 더 붙이지 않는다.
        # 그 뒤에 채팅이 화제를 바꾸면 새 화제의 몫(Thread.free_points)은 새로 생긴다
        self._run.thread.free_points = self._run.thread.points_left = 0
        return self._run.block

    def _open_thread(self, material: Material, block: BlockScript | None = None) -> None:
        """화제를 꺼내는 단계 — 이번 진입의 갈래를 정하고 질문·다룬 갈래·시간 맥락을 붙인다 (CORNER_DESIGN 5.1.1절)."""
        now = time.time()
        material.extra["now"] = time_context(now + self.clock_offset_sec,
                                             (self._started_at or now) + self.clock_offset_sec)
        run = self._run
        song_story = None
        if block is not None:
            material.extra["parts"] = block.parts
            self._quiet_visits += 1
        if run is not None and run.after_song and self._last_track is not None:
            material.extra["after_song"] = self._last_track
            self._song_returns += 1
            every = self.chat_rules.song_story_every
            # 주제 이야기만 되풀이하지 않게 곡 이야기로 쉬어 간다. 방금 나간 곡으로 한다 — 다음 곡은 백엔드가
            # 앞 곡 소개를 받은 뒤에야 알려 주는데, 이 덩어리는 그 전에 쓴다
            if block is not None and every and self._quiet_visits % every == 0:
                song_story = self._last_track
            # 곡을 받는 말도 매번 하면 틀이 된다. 곡 이야기를 하는 차례에는 그 이야기가 곡을 받는다
            material.extra["back_announce"] = song_story is None and self._song_returns % 2 == 1
        # 화제를 꺼내면서 묻지 않는다 — 질문은 곡 앞에서 던지고 답은 곡 뒤에 받는다 (_should_ask, 7.7.2절)
        material.extra["no_question"] = True
        if song_story is not None:
            material.extra["song_story"] = song_story
            return
        if run is None:
            return
        if block is not None and (aired := self._take_callback(run, now)) is not None:
            material.extra["callback"] = aired
            self.telemetry.log_callback(aired)
        if self.topic_plan is None:
            return
        covered = [a.title for a in self.topic_plan.used]
        angle, state = self.topic_plan.take(self.chat_rules.block_points if block is not None else 1)
        run.thread.angle = angle
        if angle is not None:  # 한 번에 쓰지 않을 때는 첫 말할 거리만 — 나머지는 이야기 단계가 하나씩 꺼낸다
            material.extra["angle"] = angle
            material.extra["angle_state"] = state
            if state != NEW:  # 하던 갈래 — 다룬 갈래 목록에서 뺀다
                covered = covered[:-1]
        if covered:
            material.extra["covered_angles"] = covered

    def _should_ask(self, material: Material, now: float) -> bool:
        """이 곡 소개에서 청취자에게 물을지 — 곡이 답을 치는 시간을 가려 주고, 곡 뒤 첫 이야기 멘트가 답을 받는다.

        이야기 코너 바로 뒤의 곡 앞에서만 묻는다(물을 거리가 방금 한 이야기다). 신청곡 소개와 더미 곡(실제로
        나가지 않는다) 앞에서는 묻지 않는다. 곡 뒤에 다른 코너가 올 차례여도 묻는다 — 그때는 이야기 코너가
        먼저 들어와 답을 받는다(_answer_corner).
        """
        track = material.track or {}
        if (not self.intensity.question or self._asked is not None or self._ask_pending
                or material.feature_ref or track.get("requester_nickname") or track.get("dummy")):
            return False
        rules = self.chat_rules
        gap = rules.question_gap_sec if self._is_quiet(now) else rules.question_active_gap_sec
        if now - self._last_question_at < gap:
            return False
        song_sec = int(track.get("duration_ms") or 0) / 1000
        if self._remaining_sec(now) < song_sec + self.clock.closing_sec() + _ANSWER_ROOM_SEC:
            return False
        return self._talks(self._corner_before)

    def _talks(self, corner_type: str | None) -> bool:
        """이야기 단계(talk_point)가 있는 코너."""
        corner = self.fmt.corners.get(corner_type) if corner_type else None
        return corner is not None and any(step.items == "talk_point" for step in corner.steps)

    def _answer_corner(self) -> ClockEntry | None:
        """받지 않은 질문이 있는데 다음 차례가 이야기 코너가 아니면, 질문이 나온 이야기 코너를 먼저 넣는다.

        곡이 나가는 동안 문자가 오면 곡 뒤 차례가 문자 소개가 된다 — 그대로 두면 답은 질문 없이 따로 인용되고
        질문은 몇 분 뒤에야 돌아온다 (시험 실행 20261119-133632). 클락의 순환은 건드리지 않는다.
        """
        asked = self._asked
        if (asked is None or asked.corner not in self.fmt.corners or self._talk_comes_next()
                or time.time() - asked.at > self.chat_rules.question_open_sec):
            return None
        return ClockEntry(self.fmt.corner(asked.corner))

    def _talk_comes_next(self) -> bool:
        """클락의 다음 코너가 이야기 코너다 — 진입 조건을 못 채울 코너는 대체 코너로 보거나 건너뛴다."""
        for entry in self.clock.upcoming(4):
            corner = entry.corner
            if not self.sources.satisfies(corner, entry.label):
                if not corner.fallback:
                    continue
                corner = self.fmt.corner(corner.fallback)
            if self._plays_song(corner):  # 곡은 연달아 틀지 않는다 — 그 뒤 코너를 본다
                continue
            return self._talks(corner.corner_type)
        return False

    def _waits_for_answers(self, now: float) -> bool:
        """질문을 던진 곡이 끝나 갈 때까지 곡 뒤 멘트를 만들지 않는다.

        두면 곡 소개를 만드는 동안 곡 뒤 멘트까지 만든다(동시 생성) — 답이 오기 전에 대본이 정해진다.
        곡이 끝나기 target_buffer_sec 전부터 만든다. 답을 여러 건 받는 멘트는 길어서 합성이 15초 넘게 걸린다 —
        20초 전에 만들기 시작했을 때는 곡 뒤에 무음이 8~12초 났다 (시험 실행 20261119-133632).
        타임라인의 시각은 청취자가 듣는 시각이라 방송 지연만큼 뺀다.
        """
        asked = self._asked
        if asked is None:
            return False
        end = self.timeline.track_end(asked.anchor_id)
        if end is None:  # 아직 제출되지 않았다(앞 순번을 기다린다) — 잠깐만 기다린다
            return now - asked.at < _ASK_PUBLISH_WAIT_SEC
        return now < end - self._air_delay_sec - self.target_buffer_sec

    def _waits_for_song(self, now: float) -> bool:
        """청취자가 있으면 곡 뒤 첫 이야기 멘트를 곡이 끝나 갈 때 만든다 — 곡이 나가는 동안 온 반응을 받는다.

        두면 곡 소개와 함께 미리 만들어, 곡 감상과 곡 직전의 말에 대한 반응이 곡 뒤 첫 멘트에 실리지 못한다.
        곡 뒤 차례가 이야기 코너일 때만 기다린다(문자 소개처럼 긴 낭독은 합성이 오래 걸린다). 조용한 방송은
        받을 반응이 없으니 기다리지 않는다. 시각 계산은 _waits_for_answers와 같다.
        """
        hold = self._song_hold
        if hold is None or self._is_quiet(now) or not self._talk_comes_next():
            return False
        anchor_id, built_at = hold
        end = self.timeline.track_end(anchor_id)
        if end is None:  # 곡 소개가 아직 제출되지 않았다 — 잠깐만 기다린다
            return now - built_at < _ASK_PUBLISH_WAIT_SEC
        return now < end - self._air_delay_sec - self.target_buffer_sec

    def _attach_asked(self, material: Material) -> None:
        """곡 전에 던진 질문이 있으면 이 멘트가 받는다. 너무 오래된 질문은 접는다."""
        asked, self._asked = self._asked, None
        if asked is None:
            return
        if time.time() - asked.at > self.chat_rules.question_open_sec:
            self.telemetry.log_question("dropped", asked.text)
            return
        material.extra["asked"] = asked.text
        # 질문이 청취자에게 들리기 시작한 때부터 온 글이 답이다. 질문을 만든 때부터 세면, 질문이 나가기 전에(만들고
        # 20초쯤 뒤에 나간다) 온 글이 답으로 소개된다 (본 실행 20261119-164247: 반응의 대상 문제 12건 중 5건)
        material.extra["asked_at"] = max(asked.at, self.timeline.started_at(asked.segment_id) or 0.0)
        if self._run is not None:
            self._run.asked = asked.text
        self.telemetry.log_question("answered", asked.text)

    def _talk_point(self, run: CornerRun) -> dict | None:
        """이야기 단계의 소재 하나 — 인용할 채팅 → 다시 꺼낼 청취자 이야기 → 갈래의 말할 거리 (CORNER_DESIGN 7.7절).

        None이면 할 말이 떨어졌다 — 이야기 단계를 끝내고 다음 코너(곡)로 간다. 억지로 채우지 않는다.
        한 번에 쓴 덩어리가 있으면 그 문단을 먼저 다 낸다. 그 사이에 온 채팅은 덩어리 바로 뒤에 인용한다.
        """
        if run.block is not None and run.block_next < run.block.parts:
            run.block_next += 1
            return {"source": "block", "block": run.block, "part": run.block_next - 1}
        # 반응 분위기는 이야기 단계를 열지 않는다 — 곡 뒤 첫 멘트가 전한다 (_build_job)
        if self._quote_ready(run.spec.mode("chat"), "talk"):
            return {"source": "chat"}
        return self._quiet_point(run)

    def _quiet_point(self, run: CornerRun | None) -> dict | None:
        """채팅 없이 말할 거리."""
        if run is None:
            return None
        thread = run.thread
        if thread.angle is not None:
            if thread.points_left > 0 and self.topic_plan is not None and (points := self.topic_plan.more()):
                thread.points_left -= 1
                return {"source": "angle", "point": points[0]}
        elif thread.free_points > 0:  # 갈래가 없다(계획 실패·다 씀·화제 전환) — 주제만으로 정해진 횟수
            thread.free_points -= 1
            return {"source": "topic"}
        # 할 말이 떨어졌다 — 앞서 나온 청취자 이야기를 꺼낸다. 갈래보다 먼저 꺼내던 때는 코너에 들어올 때마다
        # "아까 ~라고 해 주셨죠"가 나왔다 (2026-11-15 실제 방송).
        # 덩어리를 쓴 진입은 다시 꺼내기도 덩어리 안에서만 한다 — 문단 뒤에 멘트를 더 붙이지 않는다
        if run.block is None and (aired := self._take_callback(run, time.time())) is not None:
            return {"source": "callback", "callback": aired}
        return None

    def _take_callback(self, run: CornerRun, now: float):
        """다시 꺼낼 청취자 이야기 — 한 코너에 한 번, 방송 전체로는 callback_gap_sec에 한 번.

        운영 설정은 꺼 두었다(chat_rules.callbacks, 2026-11-19) — 그때는 엔딩 돌아보기만 청취자 이야기를 다시 꺼낸다.
        """
        if (not self.chat_rules.callbacks or run.called_back
                or now - self._last_callback_at < self.chat_rules.callback_gap_sec):
            return None
        aired = self.callbacks.take(now)
        if aired is not None:
            run.called_back = True
            self._last_callback_at = now
        return aired

    def _use_point(self, point: dict, material: Material) -> None:
        material.extra["material"] = point["source"]
        if point["source"] != "chat" and self._is_quiet(time.time()):
            material.extra["no_question"] = True  # 답이 없는 방송에서 멘트마다 묻지 않는다
        if point["source"] == "callback":
            material.extra["callback"] = point["callback"]
            self.telemetry.log_callback(point["callback"])
        elif point["source"] == "angle" and self._run is not None and self._run.thread.angle:
            material.extra["angle"] = Angle(self._run.thread.angle.title, (point["point"],))

    def _next_anchor_id(self, kind: str) -> str:
        self._anchor_no += 1
        return f"{kind}:{self._anchor_no}"

    def _anchor_for(self, planned: PlannedStep) -> Anchor:
        """이 단계가 다루는 것. 곡 소개와 그 곡은 같은 앵커다. 접착 문구는 앵커를 바꾸지 않는다."""
        source, items = planned.step.items, planned.items
        if source == "track" and items:
            track = items[0]
            anchor = self.timeline.new_anchor(
                self._next_anchor_id("track"), "track",
                keywords(f"{track.get('title', '')} {track.get('artist', '')}"))
        elif source in ("board_story", "message") and items:
            kind = "story" if source == "board_story" else "message"
            anchor = self.timeline.new_anchor(self._next_anchor_id(kind), kind,
                                              keywords(items[0].get("body")))
        else:
            if self._run_anchor is None:
                words = self._run.thread.keywords if self._run else keywords(self.topic)
                self._run_anchor = self.timeline.new_anchor(self._next_anchor_id("topic"), "topic", words)
            anchor = self._run_anchor
        self._last_anchor = anchor
        return anchor

    async def _expire_inputs(self, now: float) -> None:
        seq = self._last_anchor.seq if self._last_anchor else None
        await self._mark_expired(self.pool.expire(now, seq))

    async def _mark_expired(self, requests: list[ListenerRequest]) -> None:
        for req in requests:
            await self._transition(req, RequestState.EXPIRED)

    def _quote_limit(self, with_feature: bool = False, step_kind: str | None = None) -> int:
        """이 멘트에 실을 수 있는 채팅 수 — 참여 강도 상한과 계약이 실을 수 있는 수 가운데 작은 쪽."""
        if self._contract_version < 2:
            return 0 if with_feature else min(self.intensity.quote_limit, _V1_QUOTES_PER_SEGMENT)
        if with_feature or step_kind in _SINGLE_QUOTE_STEPS:
            return min(self.intensity.quote_limit, _QUOTES_WITH_FEATURE)
        return self.intensity.quote_limit

    def _may_quote(self, chat_mode, step_kind: str, with_feature: bool = False) -> bool:
        """이 단계가 채팅을 받을 수 있다 — 코너 계약과 참여 강도가 허용한다."""
        return (chat_mode in QUOTABLE and STEP_KINDS[step_kind].quotable
                and self._quote_limit(with_feature) > 0)

    def _quote_ready(self, chat_mode, step_kind: str, with_feature: bool = False) -> bool:
        """지금 이 단계에 채팅을 인용할 수 있다 — 풀에 채팅이 있고 계약·참여 강도·인용 간격이 허용한다."""
        return (bool(self.pool.quotable()) and self._may_quote(chat_mode, step_kind, with_feature)
                and time.time() - self._last_quote_at >= self.intensity.quote_gap_sec)

    def _take_quotes(self, chat_mode, step_kind: str, anchor: Anchor,
                     material: Material) -> list[ListenerRequest]:
        """코너 계약과 참여 강도가 허용하면 선별기가 고른다 (CORNER_DESIGN 5.1, 5.7절)."""
        # 계약 v1의 request_ref는 하나뿐이다 — v2로 보낼 때만 주 소재와 함께, 여러 건을 싣는다
        with_feature = bool(material.feature_ref)
        asked_at = material.extra.get("asked_at")
        # 답은 질문에 기대는 글이다 — 한 건씩 받을 때를 놓친 글(분위기로만 전할 글)도 답 후보로는 본다
        replies = (self.pool.answerable(asked_at)
                   if asked_at and self._contract_version >= 2 and self._may_quote(chat_mode, step_kind, with_feature)
                   else [])
        if not replies and not self._quote_ready(chat_mode, step_kind, with_feature):
            return []
        limit = self._quote_limit(with_feature, step_kind)
        now = time.time()
        topic_words = anchor.keywords | keywords(material.topic)
        candidates, last_requester = self.pool.quotable(), self._last_requester
        if replies and limit:
            # 질문을 던진 뒤에 온 글을 후보로 넉넉히 주고, 답인 것만 대본이 골라 받는다 (steps.ANSWER_QUOTE_RULE).
            # 먼저 와 있던 글은 다음 멘트가 받는다 — 가리지 않았을 때는 답을 두고 그 글들을 받았다 (20261119-133632).
            # 방금 인용한 사람의 답도 받는다 — 연달아 받지 않는 규칙에 걸려 하나뿐인 답을 놓쳤다 (20261119-143036)
            candidates, limit, last_requester = replies, _ANSWER_CANDIDATES, None
            topic_words |= keywords(material.extra.get("asked"))
            material.extra["optional_quotes"] = True
        selection = self.selector.select(
            candidates, limit=limit, arrivals=self.pool.recent_arrivals(now),
            last_requester=last_requester, topic_words=topic_words)
        self.telemetry.log_selection(selection, anchor.id)
        material.extra["selection"] = selection
        if not selection.picked:
            return []
        self._last_quote_at = now
        self._last_requester = selection.picked[-1].requester_ref
        for req in selection.picked:
            self.pool.remove(req)
        return selection.picked

    def _mood_ready(self, chat_mode, step_kind: str, with_feature: bool = False) -> bool:
        """지금 이 단계에 반응 분위기를 전할 수 있다 — 반응이 와 있고 코너 계약·참여 강도·간격이 허용한다.

        주 소재(사연·신청곡)가 있는 멘트에는 붙이지 않는다 — 그 멘트는 그 청취자의 것이다.
        """
        return (bool(self.pool.reactions()) and chat_mode in QUOTABLE and STEP_KINDS[step_kind].quotable
                and not with_feature and self.intensity.quote_limit > 0
                and time.time() - self._last_mood_at >= self.chat_rules.mood_gap_sec)

    def _take_mood(self, chat_mode, step_kind: str, material: Material) -> None:
        """와 있는 반응을 모두 이 멘트에 싣는다 — 한 건씩 인용하지 않고 분위기로 전한다 (CORNER_DESIGN 5.1.3절)."""
        if not self._mood_ready(chat_mode, step_kind, bool(material.feature_ref)):
            return
        self._last_mood_at = time.time()
        material.mood = self.pool.reactions()
        for req in material.mood:
            self.pool.remove(req)

    def _mark_steer(self, material: Material) -> None:
        """고른 채팅이 화제를 바꿀 조건을 채우면 표시한다. 화제는 인용이 대본에 들어간 뒤에 바꾼다."""
        if not material.quotes or self._run is None:
            return
        if material.quotes[0].tone == TONE_JOKE:  # 농담은 한마디로 받고 넘긴다 — 화제로 삼으면 장난에 끌려간다
            return
        rule = steer_rule(material.quotes[0].body, self.chat_rules, self.config.profile.dj_name)
        if rule and self._run.can_steer(self.intensity.steer_limit):
            material.extra["steer"] = rule
            self._steer_pending = True

    def _mark_aggregate(self, chat_mode, step_kind: str, material: Material) -> None:
        """인용·주 소재가 없는 멘트에 집계 언급이나 예고를 하나 붙인다 (CORNER_DESIGN 9.2절).

        인용과 같이 넣지 않는다 — 멘트가 길어지면 다른 청취자의 방송 시간을 쓴다.
        """
        if (not self._aggregate_ack or self._run is None or material.quotes or material.mood
                or material.feature_ref
                or chat_mode not in QUOTABLE or not STEP_KINDS[step_kind].quotable):
            return
        now = time.time()
        if not self._run.aggregated:
            trend = chat_trend(self.pool.items, now=now, window_sec=self.pool.reaction_window_sec,
                               rules=self.chat_rules, violates=self._violates)
            if trend is not None:
                self._run.aggregated = True
                material.extra["aggregate"] = trend
                return
        if self._should_preview(now):
            self._previewed = True
            material.extra["preview"] = {"title": self.fmt.corner(_TEXT_CORNER).title,
                                         "messages": self.sources.inbox.count()}

    def _should_preview(self, now: float) -> bool:
        rules = self.chat_rules
        return (not self._previewed and _TEXT_CORNER in self.fmt.corners
                and self.sources.inbox.count() >= rules.preview_min_messages
                # 곧 끝나는 방송에서는 읽어 준다고 약속할 수 없다
                and self._remaining_sec(now) >= rules.preview_min_remaining_sec
                and all(entry.corner.corner_type != _TEXT_CORNER
                        for entry in self.clock.upcoming(rules.preview_lookahead)))

    def _log_aggregate(self, job: GenerationJob, segment_id: str) -> None:
        extra = job.material.extra
        if job.material.mood:
            self.telemetry.log_aggregate(
                segment_id=segment_id, kind="mood", corner_type=job.corner_type, keyword=None,
                count=len({req.requester_ref for req in job.material.mood}), total=None)
        if trend := extra.get("aggregate"):
            self.telemetry.log_aggregate(
                segment_id=segment_id, kind="aggregate", corner_type=job.corner_type,
                keyword=trend.keyword, count=trend.people, total=trend.total_people)
        elif preview := extra.get("preview"):
            self.telemetry.log_aggregate(
                segment_id=segment_id, kind="preview", corner_type=job.corner_type,
                keyword=None, count=preview["messages"], total=None)

    def _apply_steer(self, req: ListenerRequest, job: GenerationJob, segment_id: str) -> None:
        """다음 단계부터 이 채팅이 화제다 (CORNER_DESIGN 5.2절). 앵커도 새 화제로 새로 만든다."""
        run = self._run
        old_topic = run.thread.topic
        # 바뀐 화제에는 갈래가 없다 — 그 화제로 정해진 횟수(chat_rules.steer_points)만 이야기한다
        run.thread = Thread(topic=req.body, keywords=sorted(keywords(req.body)),
                            free_points=self.chat_rules.steer_points, steered=True)
        run.steers += 1
        self._run_anchor = None
        self.telemetry.log_steer(request_id=req.request_id, segment_id=segment_id,
                                 corner_type=job.corner_type, rule=job.material.extra["steer"],
                                 old_topic=old_topic, new_topic=req.body)

    # ── 생성 · 제출 ──────────────────────────────────────────────────

    async def _publish_glue(self, ctx: ScheduleContext, planned: PlannedStep) -> None:
        corner = planned.corner.corner_type
        audio_ref, duration_ms = self.glue.get(corner, planned.step.glue)
        self.telemetry.log_decision(ctx, Decision(action="glue", corner_type=corner,
                                                  step=f"glue:{planned.step.glue}"))
        sub = SegmentSubmission(
            id=new_id("seg"), station_id=self.config.station_id, audio_ref=audio_ref,
            duration_ms=duration_ms, corner_type=corner, kind=WIRE_KIND,
            priority=50, reorderable=False,
        )
        await self._complete(self._reserve(), sub, anchor=self._last_anchor)

    async def _generate(self, job: GenerationJob, seq: int) -> None:
        self._inflight += 1
        sub: SegmentSubmission | None = None
        inputs = _inputs(job)
        for req in inputs:
            self._generating[req.request_id] = req
        try:
            async with self._sem:
                if seq in self._abandoned:  # 차례를 기다리는 사이 코너가 바뀌었다 — 입력은 이미 되돌렸다
                    return
                for req in inputs:
                    await self._transition(req, RequestState.GENERATING)
                if seq in self._abandoned:
                    return
                sub = await self.pipeline.run(job)
                if seq in self._abandoned:  # 만드는 사이 코너가 바뀌었다 — 결과를 버린다
                    if sub is not None:
                        self.pipeline.take_aired(sub.id)
                    sub = None
                    return
                self._settling.add(seq)
                if any(req.request_id in self._discard for req in inputs):
                    sub = None  # 취소·철회됐다 — 만든 결과를 내보내지 않는다
                    # 취소된 신청곡이 아니면 곡은 잘못이 없다 — 다음 곡 소개에 쓴다
                    if not (job.feature and job.feature.request_id in self._discard):
                        self._put_back_track(job)
                    await self._settle_discarded(inputs, job)
                    return
                if sub is None:  # L1/L2 REJECT — 인용한 입력 때문일 수 있다
                    self._put_back_track(job)
                    self._drop_round(job, requeue=False)  # 걸린 고민은 다시 쓰지 않는다
                    for req in inputs:
                        await self._transition(req, RequestState.REJECTED)  # F-15
                    return
                if not job.provider_failed:  # 고정 문구로 대체됐으면 언급이 나가지 않았다
                    self._log_aggregate(job, sub.id)
                    self._note_asked(job, sub.id)
                # 대본에 실제로 들어간 입력만 반영된 것이다. 나머지는 인용이 빠졌거나 filler로 대체됐다
                refs = sub.input_refs or ([sub.request_ref] if sub.request_ref else [])
                reflected = [req for req in inputs if req.request_id in refs]
                held = {req.request_id for req in reflected}
                sub.input_refs = [ref for ref in refs if ref in held]  # 파이프라인이 실은 순서 — 첫 건이 request_ref다
                sub.reflection_levels = {req.request_id: _reflection_level(req, job) for req in reflected}
                if reflected:
                    self._quoted[sub.id] = (reflected, job)
                for req in inputs:
                    if req in reflected:
                        await self._transition(req, RequestState.GENERATED)
                    elif job.material.extra.get("optional_quotes") and req in job.material.quotes:
                        if leans(req):  # 질문에 기대는 글인데 답으로 받지 않았다 — 받을 자리가 지났다
                            await self._transition(req, RequestState.EXPIRED)
                        else:  # 답이 아니라서 받지 않았다 — 실패로 세지 않고 되돌린다
                            await self._return_input(req)
                    else:
                        await self._requeue_or_reject(req, job)
                steering = _steering_quote(job)
                if steering in reflected and seq not in self._abandoned:
                    self._apply_steer(steering, job, sub.id)
        except Exception:
            # 생성 태스크는 아무도 await하지 않으므로 여기서 기록하지 않으면 실패가 사라진다.
            # 파이프라인이 재시도·filler 대체까지 했는데도 실패한 경우다 (TTS 전면 장애 등)
            logger.exception("GENERATION_FAILED %s %s/%s", job.job_id, job.corner_type, job.step)
            sub = None
            if seq not in self._abandoned or seq in self._settling:
                self._put_back_track(job)
                self._drop_round(job)
                for req in inputs:
                    if req.state == RequestState.GENERATING:
                        await self._requeue_or_reject(req, job)
        finally:
            self._jobs.pop(seq, None)
            settled = seq in self._settling
            self._settling.discard(seq)
            if seq in self._abandoned:
                # 순번·동시 생성 수는 코너를 바꿀 때 정리했다. 결과를 정리하던 중이었으면 여기서 되돌린다
                self._abandoned.discard(seq)
                if settled and sub is not None:
                    await self._take_back(sub, job)
            else:
                self._inflight -= 1
                if job.material.extra.get("steer"):
                    self._steer_pending = False
                if job.material.extra.get("ask"):
                    self._ask_pending = False
                for req in inputs:
                    self._generating.pop(req.request_id, None)
                # 실패해도 순번은 비워야 뒤 순번이 나간다. 곡 길이는 백엔드가 실제로 틀 곡(music_ref)만 센다
                track_ms = int((job.material.track or {}).get("duration_ms") or 0)
                await self._complete(seq, sub, track_ms if sub and sub.music_ref else 0, job.anchor, job)

    def _note_asked(self, job: GenerationJob, segment_id: str) -> None:
        """질문을 던지라고 한 곡 소개가 실제로 물었으면 그 질문을 적어 둔다 — 곡 뒤 멘트가 받는다."""
        if not job.material.extra.get("ask"):
            return
        # 시험용 파이프라인에는 대본을 묻는 길이 없을 수 있다 — 없으면 묻지 않은 것으로 둔다
        question = question_of(getattr(self.pipeline, "script_of", lambda _id: None)(segment_id) or "")
        if question:
            self._asked = Asked(segment_id, question, time.time(), job.anchor.id if job.anchor else None,
                                job.material.extra.get("ask_corner"))
            self._questions.append(question)
            self.telemetry.log_question("asked", question)

    def _put_back_track(self, job: GenerationJob) -> None:
        """곡 소개 멘트가 나가지 못했다 — 소개하려던 곡을 다음 곡 소개 차례로 돌린다 (#216)."""
        if job.material.track:
            self.sources.tracks.put_back(job.material.track)

    async def _settle_discarded(self, inputs: list[ListenerRequest], job: GenerationJob) -> None:
        for req in inputs:
            if req.request_id in self._discard:
                self._discard.discard(req.request_id)
                await self._transition(req, RequestState.CANCELLED)
            else:  # 함께 들어간 입력은 잘못이 없다
                job.provider_failed = True
                await self._requeue_or_reject(req, job)

    def _drop_round(self, job: GenerationJob, *, requeue: bool = True) -> None:
        """고민을 소개하는 멘트가 나가지 못했다 — 그 라운드를 접는다. 남은 단계(이야기·결과 공개)는 건너뛴다."""
        votes = self.sources.votes
        if job.step == "game_ask" and votes.active is not None and votes.active is job.material.extra.get("round"):
            votes.abandon(requeue=requeue)

    async def _take_back(self, sub: SegmentSubmission, job: GenerationJob | None) -> int:
        """만들었지만 방송에 나가지 않게 된 세그먼트 — 소개하려던 곡과 인용한 입력을 되돌린다 (#259).

        되돌린 입력 수를 돌려준다.
        """
        self._unaired.pop(sub.id, None)
        if self._asked is not None and self._asked.segment_id == sub.id:  # 질문을 던진 멘트가 나가지 않았다
            self._asked = None
        self.sources.votes.discarded(sub.id)  # 투표를 열 멘트였으면 그 투표는 열리지 않는다
        self.pipeline.take_aired(sub.id)
        if job is not None:
            self._put_back_track(job)
        quoted = self._quoted.pop(sub.id, None)
        if quoted is None:
            return 0
        for req in quoted[0]:
            await self._return_input(req)
        return len(quoted[0])

    async def _return_input(self, req: ListenerRequest) -> None:
        """코너 변경으로 나가지 못한 입력 — 입력 탓이 아니므로 실패로 세지 않고 제자리로 되돌린다 (#259)."""
        if req.request_id in self._discard:  # 생성 중에 취소·철회됐다
            self._discard.discard(req.request_id)
            await self._transition(req, RequestState.CANCELLED)
        elif req.kind == SONG_REQUEST:  # 곡도 함께 되돌아간다 — 다음 곡 소개가 한마디를 다시 쓴다
            self._song_requests[req.request_id] = req
            await self._transition(req, RequestState.INBOXED)
        else:
            if req.kind not in (MESSAGE, BOARD_STORY):
                self._forget_quote(req)
                self._last_quote_at = float("-inf")  # 인용이 나가지 않았다 — 인용 간격을 다시 세지 않는다
            await self._put_back_input(req)

    def _forget_quote(self, req: ListenerRequest) -> None:
        self.selector.forget(req)
        if self._last_requester == req.requester_ref:  # 인용이 나가지 않았으니 연속 금지에 걸지 않는다
            self._last_requester = None

    async def _put_back_input(self, req: ListenerRequest) -> None:
        """입력을 꺼내 온 곳으로 되돌린다."""
        if req.kind == BOARD_STORY:  # 상태를 알릴 곳이 없다
            self.sources.stories.put_back(req)
            await self._transition(req, RequestState.QUEUED)
        elif req.kind == MESSAGE:
            self.sources.inbox.put_back(req)
            await self._transition(req, RequestState.INBOXED)
        else:
            await self._transition(req, RequestState.QUEUED)
            self.pool.put_back(req)

    async def _requeue_or_reject(self, req: ListenerRequest, job: GenerationJob) -> None:
        if req.kind == SONG_REQUEST:  # 곡은 소개 실패와 상관없이 나간다 — 한마디만 다시 말할 차례가 없다
            await self._transition(req, RequestState.EXPIRED)
            return
        if req.kind == BOARD_STORY:  # 장애가 지나면 다시 고를 수 있다
            await self._put_back_input(req)
            return
        if req.kind != MESSAGE:
            self._forget_quote(req)
        # 제공자 장애는 입력 탓이 아니다 — 세지 않고 되돌려, 창이 지나면 만료되게 둔다
        failures = self._failures.get(req.request_id, 0) + (0 if job.provider_failed else 1)
        self._failures[req.request_id] = failures
        if failures >= _MAX_REQUEST_FAILURES:
            await self._transition(req, RequestState.REJECTED)
            return
        await self._put_back_input(req)

    async def _transition(self, req: ListenerRequest, state: RequestState) -> None:
        req.state = state
        self.telemetry.log_request_state(req.request_id, state)
        wire = wire_state(state, self._contract_version)
        if wire is not None and req.kind != BOARD_STORY:  # 게시판 사연은 요청이 아니다 (계약 3.5절)
            await self.transport.notify_request_state(req.request_id, wire)

    def _reserve(self) -> int:
        seq = self._seq
        self._seq += 1
        if self._run is not None:  # 이 순번을 편성한 코너 — 끝나면 그 코너의 예산에 더한다
            self._seq_run[seq] = self._run
            self._run.pending += 1
        return seq

    def _spend(self, seq: int, sub: SegmentSubmission | None) -> None:
        """순번 seq의 멘트 길이를 그것을 편성한 코너에 더한다. 곡 길이는 넣지 않는다 (#220)."""
        run = self._seq_run.pop(seq, None)
        if run is None:
            return
        run.pending -= 1
        if sub is not None:
            run.spent_ms += sub.duration_ms
        if run.closed and run.pending == 0:
            self._log_corner_length(run)

    def _log_corner_length(self, run: CornerRun) -> None:
        self.telemetry.log_corner(run.spec.corner_type, run.label, "length", run.spent_ms,
                                  run.spec.title)

    async def _complete(self, seq: int, sub: SegmentSubmission | None, track_ms: int = 0,
                        anchor: Anchor | None = None, job: GenerationJob | None = None) -> None:
        """순번 seq의 결과를 받아, 앞 순번이 다 나갔으면 차례대로 제출한다."""
        self._spend(seq, sub)
        self._ready[seq] = (sub, track_ms, anchor, job)
        async with self._pub_lock:
            while self._next_pub in self._ready:
                ready, ms, seg_anchor, seg_job = self._ready.pop(self._next_pub)
                self._next_pub += 1
                if ready is not None:
                    await self._publish(ready, ms, seg_anchor, seg_job)

    async def _announce_corner(self, sub: SegmentSubmission) -> None:
        """코너 첫 세그먼트 제출 전에 알린다. 백엔드가 그 세그먼트 재생 시작에 화면을 바꾼다 (계약 10.7절)."""
        corner = self.fmt.corners.get(sub.corner_type)
        if (not self._emit_corner_changed or corner is None or sub.kind == SegmentKind.FILLER
                or sub.corner_type == self._announced_corner):
            return
        self._announced_corner = sub.corner_type
        self._announced_segment = sub.id
        payload = {
            "corner": sub.corner_type, "title": corner.title, "segment_id": sub.id,
            "contract": {kind: str(corner.mode(kind)) for kind in (CHAT, MESSAGE, SONG_REQUEST)},
        }
        if self._emit_corner_format:  # 방송 중에 형식이 바뀌면 화면도 이 코너가 들릴 때 바뀌게 한다 (#260)
            payload["format"] = self.fmt.format
        await self.transport.notify_station("corner.changed", payload)

    async def _publish(self, sub: SegmentSubmission, track_ms: int = 0,
                       anchor: Anchor | None = None, job: GenerationJob | None = None) -> None:
        self._unaired[sub.id] = (sub, job, track_ms)
        if anchor is not None:  # 첫 곡 전이나 앵커 없는 접착 문구는 송출 위치를 옮기지 않는다
            track = (job.material.track or {}) if job is not None else {}
            self.timeline.append(
                sub.id, anchor, sub.duration_ms, time.time(), track_ref=sub.music_ref, track_ms=track_ms,
                # 시험용 파이프라인에는 대본을 묻는 길이 없을 수 있다 — 없으면 무엇을 들었는지 모르는 것으로 둔다
                text=getattr(self.pipeline, "script_of", lambda _id: None)(sub.id),
                track=" — ".join(v for v in (track.get("title"), track.get("artist")) if v) or None)
        # 곡 소개 멘트 뒤에는 백엔드가 그 곡을 바로 튼다 — 곡 길이만큼 방송이 채워진다
        self._produced_ms += sub.duration_ms + track_ms
        self._submitted += 1
        await self._announce_corner(sub)
        await self._open_vote(sub, job)
        await self.transport.publish_segment(sub)

    async def _open_vote(self, sub: SegmentSubmission, job: GenerationJob | None) -> None:
        """고민을 소개하는 멘트를 제출하기 전에 투표를 알린다 — 백엔드가 그 멘트의 재생 시작에 창을 연다 (계약 10.8절)."""
        round_ = job.material.extra.get("round") if job is not None and job.step == "game_ask" else None
        if round_ is None:
            return
        votes = self.sources.votes
        if job.provider_failed or sub.kind == SegmentKind.FILLER:  # 고민이 방송에 나가지 않았다 — 투표를 열지 않는다
            if votes.active is round_:
                votes.abandon()
            return
        if not votes.submitted(round_, sub.id, time.time()):
            return
        dilemma = round_.dilemma
        await self.transport.notify_station("vote.open", {
            "vote_id": round_.vote_id, "kind": "game", "question": dilemma.question,
            "options": list(dilemma.options), "segment_id": sub.id, "window_sec": int(votes.window_sec),
        })
        self.telemetry.log_vote_open(round_)

    def on_vote_result(self, vote_id: str, counts: dict[str, int]) -> None:
        """vote.result — 진행 중인 라운드의 것만 받는다. 버린 투표와 재전달은 무시한다 (계약 10.8절)."""
        self.sources.votes.result(vote_id, counts)


def _reflection_level(req: ListenerRequest, job: GenerationJob) -> int:
    """이 멘트에서 입력이 도달한 반영 단계 (기획서 4.2절, 계약 10.5절).

    인용·낭독은 ②, 다음 화제가 되었거나(STEER) 한 단계의 주 소재(게시판 사연·신청곡)면 ③이다.
    """
    if req is _steering_quote(job) or req.kind in (BOARD_STORY, SONG_REQUEST):
        return 3
    if any(req is reaction for reaction in job.material.mood):
        return _MOOD_LEVEL
    return QUOTE_LEVEL


def _steering_quote(job: GenerationJob) -> ListenerRequest | None:
    """다음 화제가 될 채팅 — 화제 전환이 표시된 멘트의 첫 인용이다 (Scheduler._mark_steer)."""
    return job.material.quotes[0] if job.material.extra.get("steer") and job.material.quotes else None


def _inputs(job: GenerationJob) -> list[ListenerRequest]:
    """이 작업이 쥐고 있는 청취자 입력 — 인용할 채팅, 분위기로 전할 반응, 주 소재(문자·사연·신청곡)."""
    return [*job.material.quotes, *job.material.mood, *([job.feature] if job.feature else [])]
