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
import time
from datetime import datetime, timedelta, timezone

from ..corners.run import CornerRun, MaterialSources, PlannedStep, Thread
from ..corners.steps import BLOCK_STEP, STEP_KINDS
from ..domain import (
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
from ..inputs.pool import QuotePool
from ..inputs.router import BOARD_STORY, CHAT, MESSAGE, SONG_REQUEST, normalize_kind
from ..inputs.rules import ChatRules
from ..inputs.selector import RELEVANCE, QuoteSelector
from ..inputs.topic_plan import NEW, Angle, TopicPlan
from ..pipeline.pipeline import GenerationPipeline
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
# v1 계약은 세그먼트당 요청 하나(request_ref)만 싣는다 — 참여 강도가 2여도 1건 (계약 v2 input_refs 전까지)
_V1_QUOTES_PER_SEGMENT = 1
_TEXT_CORNER = "text_corner"  # 예고가 가리키는 코너
# 곡 연속 방지로 연달아 건너뛸 수 있는 곡 수 (#228). 건너뛰기는 시간이 들지 않는 결정이고, 클락이 코너를
# _MAX_EMPTY_CORNERS번 연달아 못 넣어야 기다린다. 1번이면 본 코너에 문자 소개가 있는 방송(빠짐 → 곡 → 빠짐 → 곡)에서
# 곡이 여전히 이어졌다 (2026-11-07 quiet)
_MAX_SONGS_HELD = 2
_OPENERS = ("talk_open", "talk_resume")  # 코너의 화제를 꺼내는 단계 — 갈래와 시간 맥락은 여기에만 붙인다
_KST = timezone(timedelta(hours=9))  # 방송 시각은 한국 시간이다 (서머타임 없음)
_WEEKDAYS = "월화수목금토일"

logger = logging.getLogger(__name__)


def time_context(now: float, started_at: float) -> str:
    """'금요일 밤 11시 40분, 방송 18분째' — 화제를 꺼낼 때 DJ가 쓸 수 있는 시간 맥락."""
    t = datetime.fromtimestamp(now, _KST)
    part = ("밤" if t.hour in (0, 21, 22, 23) else "새벽" if t.hour < 5 else "아침" if t.hour < 9
            else "오전" if t.hour < 12 else "오후" if t.hour < 18 else "저녁")
    clock = f"{t.hour % 12 or 12}시" + (f" {t.minute}분" if t.minute else "")
    minutes = int(max(0.0, now - started_at) // 60)
    elapsed = f"방송 {minutes}분째" if minutes else "방송 시작 직후"
    return f"{_WEEKDAYS[t.weekday()]}요일 {part} {clock}, {elapsed}"


class Scheduler:
    def __init__(self, *, config: StationConfig, fmt: FormatSpec, clock: Clock,
                 sources: MaterialSources, intensity: Intensity, topic: str,
                 pipeline: GenerationPipeline, glue: GlueBank, transport: Transport,
                 telemetry: Telemetry, chat_rules: ChatRules, violates,
                 target_buffer_sec: float = 30.0, max_concurrent: int = 2,
                 emit_corner_changed: bool = False, topic_plan: TopicPlan | None = None):
        self.config = config
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
            track_end=self.timeline.track_end,
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
        self._quoted: dict[str, tuple[ListenerRequest, GenerationJob]] = {}  # segment_id -> 인용한 입력
        self._song_requests: dict[str, ListenerRequest] = {}  # 곡 차례를 기다리는 신청곡의 한마디
        self._generating: dict[str, ListenerRequest] = {}  # 생성 중인 입력
        self._discard: set[str] = set()  # 생성 중 취소·철회된 입력 — 결과를 버린다
        self._steer_pending = False  # 화제를 바꿀 인용을 생성 중 — 결과가 나와야 다음 단계의 화제가 정해진다
        self._emit_corner_changed = emit_corner_changed
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
        self._ready: dict[int, tuple[SegmentSubmission | None, int, Anchor | None]] = {}
        self._seq_run: dict[int, CornerRun] = {}  # 순번 → 그것을 편성한 코너 (코너 예산 — #220)
        self._deferred: PlannedStep | None = None  # 만들 때를 기다리는 곡 소개 (#239)
        self._pub_lock = asyncio.Lock()
        # 청취자가 적은 방송의 흐름 (CORNER_DESIGN 7.7.1절, #262)
        self._last_chat_at = float("-inf")  # 마지막 채팅 도착 — 한동안 없으면 조용한 방송이다
        self._last_question_at = float("-inf")  # 청취자에게 마지막으로 물은 때
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

    def on_segment_published(self, segment_id: str, at: float) -> None:
        """PUBLISHED — 대본을 송출 이력으로 옮긴다 (context-memory 3.1절). 재전달은 무시한다."""
        script = self.pipeline.take_aired(segment_id)
        if script is not None:
            self.telemetry.log_aired(segment_id, at, script)

    def enqueue_request(self, req: ListenerRequest) -> None:
        anchor = self.timeline.current(req.received_at)
        req.chat_type, req.rule = classify(req.body, anchor, self.chat_rules)
        if anchor is not None:
            req.anchor_id, req.anchor_seq = anchor.id, anchor.seq
        self.telemetry.log_input(req, anchor.kind if anchor else None)
        req.state = RequestState.QUEUED
        self.telemetry.log_request_state(req.request_id, req.state)
        self.pool.add(req)
        self._last_chat_at = max(self._last_chat_at, req.received_at)

    def on_track_started(self, track_ref: str, started_at: float) -> None:
        self.timeline.sync_track(track_ref, started_at)

    def on_segment_played(self, segment_id: str, at: float) -> None:
        speech_ms = self.timeline.speech_ms(segment_id)
        if speech_ms is not None:  # 모르는 세그먼트 — 백엔드가 혼자 트는 곡 등
            self.on_segment_started(segment_id, at - speech_ms / 1000)

    def on_segment_started(self, segment_id: str, started_at: float) -> None:
        """재생 시작 확인 — 인용이 들어 있으면 반영 확정 (CORNER_DESIGN 9.3, 9.4절). 재전달은 무시한다."""
        if not self.timeline.sync_started(segment_id, started_at):
            return
        quoted = self._quoted.pop(segment_id, None)
        if quoted is None:
            return
        req, job = quoted
        req.state = RequestState.REFLECTED
        self.telemetry.log_request_state(req.request_id, req.state)
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
            ctx = self._snapshot()
            blocked_sec = self.pipeline.tts_blocked_until - ctx.now
            if blocked_sec > 0:  # 합성할 수 없으니 LLM도 부르지 않는다. 입력은 창이 지나면 만료된다
                self.telemetry.log_decision(ctx, Decision(action="tts_blocked"))
                await asyncio.sleep(min(blocked_sec, _TTS_BLOCKED_SLEEP_SEC))
                continue
            if (ctx.inflight_generations >= self.max_concurrent
                    or ctx.generated_buffer_sec >= self.target_buffer_sec
                    or self._steer_pending):
                self.telemetry.log_decision(ctx, Decision(action="wait"))
                await asyncio.sleep(_IDLE_SLEEP_SEC)
                continue
            planned = self._deferred or self._next_planned(ctx)
            if planned is None:  # 엔딩까지 편성을 마쳤다 — 남은 생성이 끝나면 종료
                self.telemetry.log_decision(ctx, Decision(action="idle"))
                break
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
                asyncio.create_task(self._generate(job, seq))
            await asyncio.sleep(0)  # 생성 태스크에 실행 양보
        while self._inflight > 0:  # 진행 중 생성 마무리 대기
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
                if plays_song:
                    self._song_since_talk = True
                else:
                    self._last_talk_corner, self._song_since_talk = corner_type, False
                self.telemetry.log_corner(entry.corner.corner_type, entry.label, "enter",
                                          self._produced_ms, entry.corner.title)
            planned = self._run.next_step()
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

    def _admit(self, entry: ClockEntry) -> ClockEntry | None:
        """진입 조건(requires)을 못 채우면 대체 코너로, 대체도 없으면 이번 차례를 건너뛴다."""
        if self._holds_song(entry.corner):
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
        point = planned.items[0] if step.items == "talk_point" else None
        if point is not None:
            material.items = []
        anchor = self._anchor_for(planned)
        await self._expire_inputs(time.time())
        if point is not None and point["source"] == "block":  # 이미 쓴 덩어리의 다음 문단 — 합성만 한다
            material.extra["material"] = "block"
            return GenerationJob(
                job_id=new_id("job"), corner_type=corner.corner_type, step=BLOCK_STEP, material=material,
                plain=corner.subjectivity == "none", anchor=anchor, block=point["block"], part=point["part"],
            )
        material.quotes = self._take_quotes(corner.mode("chat"), step_kind, anchor, material)
        if point is not None:
            if point["source"] == "chat" and not material.quotes:  # 선별기가 고르지 않았다 — 다른 소재로
                point = self._quiet_point(self._run) or {"source": "topic"}
            self._use_point(point, material)
        if corner.mode("chat") == Mode.STEER:
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
        quiet = self._is_quiet(now)
        material.extra["now"] = time_context(now, self._started_at or now)
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
        ask = self.intensity.question and (
            not quiet or now - self._last_question_at >= self.chat_rules.question_gap_sec)
        material.extra["question"] = ask
        if ask:
            self._last_question_at = now
        elif quiet:
            material.extra["no_question"] = True
        if song_story is not None:
            material.extra["song_story"] = song_story
            return
        if run is None:
            return
        if block is not None and not run.called_back and (aired := self.callbacks.take(now)) is not None:
            run.called_back = True
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

    def _talk_point(self, run: CornerRun) -> dict | None:
        """이야기 단계의 소재 하나 — 인용할 채팅 → 다시 꺼낼 청취자 이야기 → 갈래의 말할 거리 (CORNER_DESIGN 7.7절).

        None이면 할 말이 떨어졌다 — 이야기 단계를 끝내고 다음 코너(곡)로 간다. 억지로 채우지 않는다.
        한 번에 쓴 덩어리가 있으면 그 문단을 먼저 다 낸다. 그 사이에 온 채팅은 덩어리 바로 뒤에 인용한다.
        """
        if run.block is not None and run.block_next < run.block.parts:
            run.block_next += 1
            return {"source": "block", "block": run.block, "part": run.block_next - 1}
        if self._quote_ready(run.spec.mode("chat"), "talk"):
            return {"source": "chat"}
        return self._quiet_point(run)

    def _quiet_point(self, run: CornerRun | None) -> dict | None:
        """채팅 없이 말할 거리."""
        if run is None:
            return None
        # 덩어리를 쓴 진입은 다시 꺼내기도 덩어리 안에서만 한다 — 문단 뒤에 멘트를 더 붙이지 않는다
        if (run.block is None and not run.called_back
                and (aired := self.callbacks.take(time.time())) is not None):
            run.called_back = True
            return {"source": "callback", "callback": aired}
        thread = run.thread
        if thread.angle is not None:
            if thread.points_left > 0 and self.topic_plan is not None and (points := self.topic_plan.more()):
                thread.points_left -= 1
                return {"source": "angle", "point": points[0]}
            return None
        if thread.free_points > 0:  # 갈래가 없다(계획 실패·다 씀·화제 전환) — 주제만으로 정해진 횟수
            thread.free_points -= 1
            return {"source": "topic"}
        return None

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

    def _quote_ready(self, chat_mode, step_kind: str) -> bool:
        """지금 이 단계에 채팅을 인용할 수 있다 — 풀에 채팅이 있고 계약·참여 강도·인용 간격이 허용한다."""
        return (bool(len(self.pool)) and chat_mode in QUOTABLE and STEP_KINDS[step_kind].quotable
                and min(self.intensity.quote_limit, _V1_QUOTES_PER_SEGMENT) > 0
                and time.time() - self._last_quote_at >= self.intensity.quote_gap_sec)

    def _take_quotes(self, chat_mode, step_kind: str, anchor: Anchor,
                     material: Material) -> list[ListenerRequest]:
        """코너 계약과 참여 강도가 허용하면 선별기가 고른다 (CORNER_DESIGN 5.1, 5.7절)."""
        # 계약 v1의 request_ref는 하나뿐이다 — input_refs 전까지 주 소재와 함께 싣지 못한다
        if material.feature_ref or not self._quote_ready(chat_mode, step_kind):
            return []
        limit = min(self.intensity.quote_limit, _V1_QUOTES_PER_SEGMENT)
        now = time.time()
        topic_words = anchor.keywords | keywords(material.topic)
        selection = self.selector.select(
            self.pool.items, limit=limit, arrivals=self.pool.recent_arrivals(now),
            last_requester=self._last_requester, topic_words=topic_words)
        self.telemetry.log_selection(selection, anchor.id)
        material.extra["selection"] = selection
        if not selection.picked:
            return []
        self._last_quote_at = now
        self._last_requester = selection.picked[-1].requester_ref
        for req in selection.picked:
            self.pool.remove(req)
        return selection.picked

    def _mark_steer(self, material: Material) -> None:
        """고른 채팅이 화제를 바꿀 조건을 채우면 표시한다. 화제는 인용이 대본에 들어간 뒤에 바꾼다."""
        if not material.quotes or self._run is None:
            return
        rule = steer_rule(material.quotes[0].body, self.chat_rules, self.config.profile.dj_name)
        if rule and self._run.can_steer(self.intensity.steer_limit):
            material.extra["steer"] = rule
            self._steer_pending = True

    def _mark_aggregate(self, chat_mode, step_kind: str, material: Material) -> None:
        """인용·주 소재가 없는 멘트에 집계 언급이나 예고를 하나 붙인다 (CORNER_DESIGN 9.2절).

        인용과 같이 넣지 않는다 — 멘트가 길어지면 다른 청취자의 방송 시간을 쓴다.
        """
        if (not self._aggregate_ack or self._run is None or material.quotes or material.feature_ref
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
        # 바뀐 화제에는 갈래가 없다 — 그 화제로 두 번까지 이야기한다
        run.thread = Thread(topic=req.body, keywords=sorted(keywords(req.body)), free_points=2)
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
        inputs = [*job.material.quotes, *([job.feature] if job.feature else [])]
        for req in inputs:
            self._generating[req.request_id] = req
        try:
            async with self._sem:
                for req in inputs:
                    await self._transition(req, RequestState.GENERATING)
                sub = await self.pipeline.run(job)
                if any(req.request_id in self._discard for req in inputs):
                    sub = None  # 취소·철회됐다 — 만든 결과를 내보내지 않는다
                    # 취소된 신청곡이 아니면 곡은 잘못이 없다 — 다음 곡 소개에 쓴다
                    if not (job.feature and job.feature.request_id in self._discard):
                        self._put_back_track(job)
                    await self._settle_discarded(inputs, job)
                    return
                if sub is None:  # L1/L2 REJECT — 인용한 입력 때문일 수 있다
                    self._put_back_track(job)
                    for req in inputs:
                        await self._transition(req, RequestState.REJECTED)  # F-15
                    return
                if not job.provider_failed:  # 고정 문구로 대체됐으면 언급이 나가지 않았다
                    self._log_aggregate(job, sub.id)
                for req in inputs:
                    if sub.request_ref == req.request_id:
                        self._quoted[sub.id] = (req, job)
                        await self._transition(req, RequestState.GENERATED)
                        if job.material.extra.get("steer"):
                            self._apply_steer(req, job, sub.id)
                    else:  # 인용이 빠졌거나 제공자 장애로 filler 대체됨 — 반영되지 않았다
                        await self._requeue_or_reject(req, job)
        except Exception:
            # 생성 태스크는 아무도 await하지 않으므로 여기서 기록하지 않으면 실패가 사라진다.
            # 파이프라인이 재시도·filler 대체까지 했는데도 실패한 경우다 (TTS 전면 장애 등)
            logger.exception("GENERATION_FAILED %s %s/%s", job.job_id, job.corner_type, job.step)
            sub = None
            self._put_back_track(job)
            for req in inputs:
                if req.state == RequestState.GENERATING:
                    await self._requeue_or_reject(req, job)
        finally:
            self._inflight -= 1
            if job.material.extra.get("steer"):
                self._steer_pending = False
            for req in inputs:
                self._generating.pop(req.request_id, None)
            # 실패해도 순번은 비워야 뒤 순번이 나간다. 곡 길이는 백엔드가 실제로 틀 곡(music_ref)만 센다
            track_ms = int((job.material.track or {}).get("duration_ms") or 0)
            await self._complete(seq, sub, track_ms if sub and sub.music_ref else 0, job.anchor)

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

    async def _requeue_or_reject(self, req: ListenerRequest, job: GenerationJob) -> None:
        if req.kind == SONG_REQUEST:  # 곡은 소개 실패와 상관없이 나간다 — 한마디만 다시 말할 차례가 없다
            await self._transition(req, RequestState.EXPIRED)
            return
        if req.kind == BOARD_STORY:  # 장애가 지나면 다시 고를 수 있다. 상태를 알릴 곳이 없다
            self.sources.stories.put_back(req)
            await self._transition(req, RequestState.QUEUED)
            return
        if req.kind != MESSAGE:
            self.selector.forget(req)
            if self._last_requester == req.requester_ref:  # 인용이 나가지 않았으니 연속 금지에 걸지 않는다
                self._last_requester = None
        # 제공자 장애는 입력 탓이 아니다 — 세지 않고 되돌려, 창이 지나면 만료되게 둔다
        failures = self._failures.get(req.request_id, 0) + (0 if job.provider_failed else 1)
        self._failures[req.request_id] = failures
        if failures >= _MAX_REQUEST_FAILURES:
            await self._transition(req, RequestState.REJECTED)
            return
        if req.kind == MESSAGE:
            self.sources.inbox.put_back(req)
            await self._transition(req, RequestState.INBOXED)
            return
        await self._transition(req, RequestState.QUEUED)
        self.pool.put_back(req)

    async def _transition(self, req: ListenerRequest, state: RequestState) -> None:
        req.state = state
        self.telemetry.log_request_state(req.request_id, state)
        wire = wire_state(state)
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
                        anchor: Anchor | None = None) -> None:
        """순번 seq의 결과를 받아, 앞 순번이 다 나갔으면 차례대로 제출한다."""
        self._spend(seq, sub)
        self._ready[seq] = (sub, track_ms, anchor)
        async with self._pub_lock:
            while self._next_pub in self._ready:
                ready, ms, seg_anchor = self._ready.pop(self._next_pub)
                self._next_pub += 1
                if ready is not None:
                    await self._publish(ready, ms, seg_anchor)

    async def _announce_corner(self, sub: SegmentSubmission) -> None:
        """코너 첫 세그먼트 제출 전에 알린다. 백엔드가 그 세그먼트 재생 시작에 화면을 바꾼다 (계약 10.7절)."""
        corner = self.fmt.corners.get(sub.corner_type)
        if (not self._emit_corner_changed or corner is None or sub.kind == SegmentKind.FILLER
                or sub.corner_type == self._announced_corner):
            return
        self._announced_corner = sub.corner_type
        await self.transport.notify_station("corner.changed", {
            "corner": sub.corner_type, "title": corner.title, "segment_id": sub.id,
            "contract": {kind: str(corner.mode(kind)) for kind in (CHAT, MESSAGE, SONG_REQUEST)},
        })

    async def _publish(self, sub: SegmentSubmission, track_ms: int = 0,
                       anchor: Anchor | None = None) -> None:
        if anchor is not None:  # 첫 곡 전이나 앵커 없는 접착 문구는 송출 위치를 옮기지 않는다
            self.timeline.append(sub.id, anchor, sub.duration_ms, time.time(),
                                 track_ref=sub.music_ref, track_ms=track_ms)
        # 곡 소개 멘트 뒤에는 백엔드가 그 곡을 바로 튼다 — 곡 길이만큼 방송이 채워진다
        self._produced_ms += sub.duration_ms + track_ms
        self._submitted += 1
        await self._announce_corner(sub)
        await self.transport.publish_segment(sub)
