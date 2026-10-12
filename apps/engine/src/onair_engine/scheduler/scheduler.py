"""편성 관리자 — 유일한 의사결정 지점 (ENGINE_ARCHITECTURE 3.1, 4.1절).

LLM은 무엇을 말할지 결정하지 않는다. 클락이 코너를, 코너가 단계를 정하고 LLM은 칸을 채운다.
편성 관리자에게 남은 일은 "지금 만들지"(버퍼 관리)와 "어떤 청취자 입력을 넣을지"다.

제출 순서는 편성 순서다 — 생성은 병렬로 하지만, 먼저 끝난 멘트가 앞 순서를 새치기하지 않게
순번(seq)대로 내보낸다. 코너 콜(접착 요소)이 이전 코너의 마지막 멘트보다 먼저 나가면 안 된다.

청취자 요청 — 입력 라우팅(InputRouter·QuoteSelector)은 M4다. 그 전까지는 과도기 처리로,
채팅 인용을 허용하는 코너의 다음 인용 가능 단계에 대기 요청 1건을 넣는다(v1 요청 상태 그대로).
요청별 방송 ack는 보내지 않는다 (CORNER_DESIGN D5).
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..corners.run import CornerRun, MaterialSources, PlannedStep, Thread
from ..corners.steps import STEP_KINDS
from ..domain import (
    Decision,
    GenerationJob,
    ListenerRequest,
    Material,
    RequestState,
    ScheduleContext,
    SegmentSubmission,
    StationConfig,
    new_id,
)
from ..format.clock import Clock, ClockEntry
from ..format.spec import QUOTABLE, FormatSpec, Intensity
from ..glue import WIRE_KIND, GlueBank
from ..pipeline.pipeline import GenerationPipeline
from ..telemetry import Telemetry
from ..transport import Transport

_IDLE_SLEEP_SEC = 0.5
_MAX_REQUEST_FAILURES = 2  # 생성 실패한 요청은 한 번 다시 큐에 넣고, 또 실패하면 거절한다
# 백엔드는 backpressure를 약 10초마다 보낸다. 세 번 연속 못 받으면 백엔드가 멈춘 것으로 보고 자체 추정으로 돌아간다
_BACKEND_REPORT_TTL_SEC = 30.0
# 클락이 코너를 연달아 이만큼 건너뛰면(전부 진입 조건 미달) 설정 오류로 보고 멈춘다
_MAX_EMPTY_CORNERS = 50
# v1 계약은 세그먼트당 요청 하나(request_ref)만 싣는다 — 참여 강도가 2여도 1건 (계약 v2 input_refs 전까지)
_V1_QUOTES_PER_SEGMENT = 1

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, *, config: StationConfig, fmt: FormatSpec, clock: Clock,
                 sources: MaterialSources, intensity: Intensity, topic: str,
                 pipeline: GenerationPipeline, glue: GlueBank, transport: Transport,
                 telemetry: Telemetry, target_buffer_sec: float = 30.0, max_concurrent: int = 2):
        self.config = config
        self.fmt = fmt
        self.clock = clock
        self.sources = sources
        self.intensity = intensity
        self.topic = topic
        self.pipeline = pipeline
        self.glue = glue
        self.transport = transport
        self.telemetry = telemetry
        self.target_buffer_sec = target_buffer_sec
        self.max_concurrent = max_concurrent
        self._sem = asyncio.Semaphore(max_concurrent)
        self._pending: list[ListenerRequest] = []
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
        self._last_quote_at = float("-inf")
        # 순서 보장 제출 — 순번 발급, 다음에 낼 순번, 끝났지만 앞 순번을 기다리는 결과
        self._seq = 0
        self._next_pub = 0
        self._ready: dict[int, tuple[SegmentSubmission | None, int]] = {}
        self._pub_lock = asyncio.Lock()

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def current_corner(self) -> str | None:
        return self._run.spec.corner_type if self._run else None

    def enqueue_request(self, req: ListenerRequest) -> None:
        req.state = RequestState.QUEUED
        self.telemetry.log_request_state(req.request_id, req.state)
        self._pending.append(req)

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
            pending_requests=list(self._pending),
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
        while not stop.is_set() and time.time() < self._end_at:
            if max_segments is not None and self._seq >= max_segments:
                break
            ctx = self._snapshot()
            if (ctx.inflight_generations >= self.max_concurrent
                    or ctx.generated_buffer_sec >= self.target_buffer_sec):
                self.telemetry.log_decision(ctx, Decision(action="wait"))
                await asyncio.sleep(_IDLE_SLEEP_SEC)
                continue
            planned = self._next_planned(ctx)
            if planned is None:  # 엔딩까지 편성을 마쳤다 — 남은 생성이 끝나면 종료
                self.telemetry.log_decision(ctx, Decision(action="idle"))
                break
            if planned.step.kind == "glue":
                await self._publish_glue(ctx, planned)
            else:
                job = self._build_job(planned)
                self.telemetry.log_decision(ctx, Decision(
                    action="generate", corner_type=job.corner_type, step=job.step,
                    quotes=[q.request_id for q in job.material.quotes]))
                seq = self._reserve()
                asyncio.create_task(self._generate(job, seq))
            await asyncio.sleep(0)  # 생성 태스크에 실행 양보
        while self._inflight > 0:  # 진행 중 생성 마무리 대기
            await asyncio.sleep(0.1)

    # ── 클락 · 코너 ──────────────────────────────────────────────────

    def _next_planned(self, ctx: ScheduleContext) -> PlannedStep | None:
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
                        logger.error("CLOCK_EMPTY %s — 진입할 수 있는 코너가 없다",
                                     self.config.station_id)
                        return None
                    continue
                self._run = CornerRun(entry, sources=self.sources, intensity=self.intensity,
                                      thread=Thread(topic=entry.label or self.topic),
                                      started_ms=self._produced_ms)
                self.telemetry.log_corner(entry.corner.corner_type, entry.label, "enter",
                                          self._produced_ms)
            planned = self._run.next_step(self._produced_ms)
            if planned is not None:
                if planned.step.kind == "glue" and self.glue.get(
                        planned.corner.corner_type, planned.step.glue) is None:
                    continue  # 이 코너는 그 문구가 없다
                return planned
            self.telemetry.log_corner(self._run.spec.corner_type, self._run.label, "exit",
                                      self._produced_ms)
            self._run = None
            empty += 1
            if empty >= _MAX_EMPTY_CORNERS:
                logger.error("CLOCK_EMPTY %s — 단계가 나오는 코너가 없다", self.config.station_id)
                return None

    def _admit(self, entry: ClockEntry) -> ClockEntry | None:
        """진입 조건(requires)을 못 채우면 대체 코너로, 대체도 없으면 이번 차례를 건너뛴다."""
        if self.sources.satisfies(entry.corner):
            return entry
        fallback = entry.corner.fallback
        if fallback and self.sources.satisfies(self.fmt.corner(fallback)):
            self.telemetry.log_corner(entry.corner.corner_type, entry.label, "fallback",
                                      self._produced_ms)
            return ClockEntry(self.fmt.corner(fallback), entry.label)
        self.telemetry.log_corner(entry.corner.corner_type, entry.label, "skip", self._produced_ms)
        return None

    def _build_job(self, planned: PlannedStep) -> GenerationJob:
        corner, step = planned.corner, planned.step
        material = Material(items=list(planned.items), label=planned.label,
                            topic=self._run.thread.topic if self._run else self.topic)
        if step.items == "track":
            material.track = planned.items[0]
            material.items = []
        if step.kind == "opening_greeting" and self.config.first_song:
            material.extra["first_song"] = self.config.first_song
        elif step.kind == "corner_preview":
            material.extra.update(guide=self.fmt.guide, corners=self.clock.main_titles())
        elif step.kind == "talk_open":
            material.extra["question"] = self.intensity.question
        elif step.kind == "news_recap":
            material.extra["covered"] = list(self.sources.covered_news)
            self.sources.covered_news.clear()  # 다음 순환은 새로 센다
        material.quotes = self._take_quotes(corner.mode("chat"), step.kind)
        return GenerationJob(
            job_id=new_id("job"), corner_type=corner.corner_type, step=step.kind,
            material=material, plain=corner.subjectivity == "none",
        )

    def _take_quotes(self, chat_mode, step_kind: str) -> list[ListenerRequest]:
        """과도기 인용 (M4 QuoteSelector 전) — 코너 계약과 참여 강도가 허용하면 가장 오래 기다린 요청."""
        if not self._pending or chat_mode not in QUOTABLE or not STEP_KINDS[step_kind].quotable:
            return []
        limit = min(self.intensity.quote_limit, _V1_QUOTES_PER_SEGMENT)
        now = time.time()
        if limit <= 0 or now - self._last_quote_at < self.intensity.quote_gap_sec:
            return []
        self._last_quote_at = now
        quotes, self._pending = self._pending[:limit], self._pending[limit:]
        return quotes

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
        await self._complete(self._reserve(), sub)

    async def _generate(self, job: GenerationJob, seq: int) -> None:
        self._inflight += 1
        sub: SegmentSubmission | None = None
        try:
            async with self._sem:
                for req in job.material.quotes:
                    await self._transition(req, RequestState.GENERATING)
                sub = await self.pipeline.run(job)
                if sub is None:  # L1/L2 REJECT — 인용한 입력 때문일 수 있다
                    for req in job.material.quotes:
                        await self._transition(req, RequestState.REJECTED)  # F-15
                    return
                for req in job.material.quotes:
                    if sub.request_ref == req.request_id:
                        await self._transition(req, RequestState.GENERATED)
                    else:  # 인용이 빠졌거나 제공자 장애로 filler 대체됨 — 반영되지 않았다
                        await self._requeue_or_reject(req)
        except Exception:
            # 생성 태스크는 아무도 await하지 않으므로 여기서 기록하지 않으면 실패가 사라진다.
            # 파이프라인이 재시도·filler 대체까지 했는데도 실패한 경우다 (TTS 전면 장애 등)
            logger.exception("GENERATION_FAILED %s %s/%s", job.job_id, job.corner_type, job.step)
            sub = None
            for req in job.material.quotes:
                if req.state == RequestState.GENERATING:
                    await self._requeue_or_reject(req)
        finally:
            self._inflight -= 1
            # 실패해도 순번은 비워야 뒤 순번이 나간다. 곡 길이는 백엔드가 실제로 틀 곡(music_ref)만 센다
            track_ms = int((job.material.track or {}).get("duration_ms") or 0)
            await self._complete(seq, sub, track_ms if sub and sub.music_ref else 0)

    async def _requeue_or_reject(self, req: ListenerRequest) -> None:
        failures = self._failures.get(req.request_id, 0) + 1
        self._failures[req.request_id] = failures
        if failures >= _MAX_REQUEST_FAILURES:
            await self._transition(req, RequestState.REJECTED)
            return
        await self._transition(req, RequestState.QUEUED)
        self._pending.insert(0, req)  # 이미 기다린 요청이므로 맨 앞으로

    async def _transition(self, req: ListenerRequest, state: RequestState) -> None:
        req.state = state
        self.telemetry.log_request_state(req.request_id, state)
        await self.transport.notify_request_state(req.request_id, state)

    def _reserve(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    async def _complete(self, seq: int, sub: SegmentSubmission | None, track_ms: int = 0) -> None:
        """순번 seq의 결과를 받아, 앞 순번이 다 나갔으면 차례대로 제출한다."""
        self._ready[seq] = (sub, track_ms)
        async with self._pub_lock:
            while self._next_pub in self._ready:
                ready, ms = self._ready.pop(self._next_pub)
                self._next_pub += 1
                if ready is not None:
                    await self._publish(ready, ms)

    async def _publish(self, sub: SegmentSubmission, track_ms: int = 0) -> None:
        # 곡 소개 멘트 뒤에는 백엔드가 그 곡을 바로 튼다 — 곡 길이만큼 방송이 채워진다
        self._produced_ms += sub.duration_ms + track_ms
        self._submitted += 1
        await self.transport.publish_segment(sub)
