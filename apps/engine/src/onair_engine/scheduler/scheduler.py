"""편성 관리자 — 유일한 의사결정 지점 (설계 문서 3.1, 4.1).

LLM은 무엇을 말할지 결정하지 않는다. 여기서 칸을 정하고 LLM은 칸을 채운다.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..ack import AckCache
from ..domain import (
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
)
from ..pipeline.pipeline import GenerationPipeline
from ..telemetry import Telemetry
from ..transport import Transport
from .policies.base import SchedulingPolicy
from .running_order import RunningOrder

_IDLE_SLEEP_SEC = 0.5
_MAX_REQUEST_FAILURES = 2  # 생성 실패한 요청은 한 번 다시 큐에 넣고, 또 실패하면 거절한다
# 백엔드는 backpressure를 약 10초마다 보낸다. 세 번 연속 못 받으면 백엔드가 멈춘 것으로 보고 자체 추정으로 돌아간다
_BACKEND_REPORT_TTL_SEC = 30.0

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, *, config: StationConfig, order: RunningOrder, policy: SchedulingPolicy,
                 corners: dict, pipeline: GenerationPipeline, ack_cache: AckCache,
                 transport: Transport, telemetry: Telemetry, max_concurrent: int = 2):
        self.config = config
        self.order = order
        self.policy = policy
        self.corners = corners
        self.pipeline = pipeline
        self.ack_cache = ack_cache
        self.transport = transport
        self.telemetry = telemetry
        self._sem = asyncio.Semaphore(max_concurrent)
        self._pending: list[ListenerRequest] = []
        self._inflight = 0
        self._produced_ms = 0
        self._submitted = 0
        self._started_at: float | None = None
        self._failures: dict[str, int] = {}  # request_id -> 생성 실패 횟수
        # 백엔드의 마지막 backpressure 보고: (D_total 초, 측정 시각, 그때까지 엔진이 제출한 ms)
        self._backend_report: tuple[float, float, int] | None = None
        self._backpressure = False

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

    def _snapshot(self) -> ScheduleContext:
        now = time.time()
        buffer_sec, source = self._buffer_sec(now)
        return ScheduleContext(
            now=now,
            pending_requests=list(self._pending),
            generated_buffer_sec=buffer_sec,
            inflight_generations=self._inflight,
            backpressure=self._backpressure,
            order_position=self.order.position,
            next_slot=self.order.peek(),
            buffer_source=source,
        )

    async def run(self, stop: asyncio.Event, max_segments: int | None = None) -> None:
        """max_segments는 관통 테스트용 — N개 제출 후 종료한다."""
        self._started_at = time.time()
        end_at = self._started_at + self.config.broadcast_minutes * 60
        while not stop.is_set() and time.time() < end_at:
            if max_segments is not None and self._submitted >= max_segments:
                break
            ctx = self._snapshot()
            decision = self.policy.decide(ctx)
            self.telemetry.log_decision(ctx, decision)  # 생략 결정 포함 전부 기록 (RQ2)
            if decision.action != "generate":
                await asyncio.sleep(_IDLE_SLEEP_SEC)
                continue
            job = await self._build_job(ctx, decision)
            asyncio.create_task(self._generate(job))
            await asyncio.sleep(0)  # 생성 태스크에 실행 양보
        while self._inflight > 0:  # 진행 중 생성 마무리 대기
            await asyncio.sleep(0.1)

    async def _build_job(self, ctx: ScheduleContext, decision: Decision) -> GenerationJob:
        if decision.request is not None:
            self._pending.remove(decision.request)
            # 되돌아온 요청은 첫 차례에 이미 ack가 나갔다 — 같은 요청에 두 번 보내지 않는다
            if decision.send_ack and decision.request.request_id not in self._failures:
                await self._publish_ack(decision.request)
            return GenerationJob(
                job_id=new_id("job"), corner_type="request_reply",
                material=Material(request=decision.request), priority=30,
            )
        corner_type = self.order.advance()
        material = await self.corners[corner_type].gather(ctx)
        if material is None:  # 소재 없음(예: RSS 캐시 빈 브리핑) -> filler 대체
            corner_type = "filler"
            material = await self.corners["filler"].gather(ctx)
        return GenerationJob(job_id=new_id("job"), corner_type=corner_type, material=material)

    async def _publish_ack(self, req: ListenerRequest) -> None:
        audio_ref, duration_ms = self.ack_cache.pick()
        sub = SegmentSubmission(
            id=new_id("seg"), station_id=self.config.station_id, audio_ref=audio_ref,
            duration_ms=duration_ms, corner_type="ack", kind=SegmentKind.ACK,
            priority=10, reorderable=False, request_ref=req.request_id,
        )
        await self._publish(sub)

    async def _generate(self, job: GenerationJob) -> None:
        self._inflight += 1
        try:
            async with self._sem:
                req = job.material.request
                if req is not None:
                    await self._transition(req, RequestState.GENERATING)
                sub = await self.pipeline.run(job)
                if sub is None:  # L1/L2 REJECT
                    if req is not None:
                        await self._transition(req, RequestState.REJECTED)  # F-15
                    return
                if req is not None:
                    if sub.request_ref == req.request_id:
                        await self._transition(req, RequestState.GENERATED)
                    else:  # 제공자 장애로 filler 대체됨 — 답변이 나간 게 아니다
                        await self._requeue_or_reject(req)
                await self._publish(sub)
        except Exception:
            # 생성 태스크는 아무도 await하지 않으므로 여기서 기록하지 않으면 실패가 사라진다.
            # 파이프라인이 재시도·filler 대체까지 했는데도 실패한 경우다 (TTS 전면 장애 등)
            logger.exception("GENERATION_FAILED %s %s", job.job_id, job.corner_type)
            req = job.material.request
            if req is not None and req.state == RequestState.GENERATING:
                await self._requeue_or_reject(req)
        finally:
            self._inflight -= 1

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

    async def _publish(self, sub: SegmentSubmission) -> None:
        self._produced_ms += sub.duration_ms
        self._submitted += 1
        await self.transport.publish_segment(sub)
