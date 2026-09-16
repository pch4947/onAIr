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

    def enqueue_request(self, req: ListenerRequest) -> None:
        req.state = RequestState.QUEUED
        self.telemetry.log_request_state(req.request_id, req.state)
        self._pending.append(req)

    def _snapshot(self) -> ScheduleContext:
        now = time.time()
        elapsed = now - (self._started_at or now)
        # 제출한 오디오 총 길이 - 경과 시간 = 방송이 앞서 있는 정도 (엔진 쪽 근사치)
        buffer_sec = max(0.0, self._produced_ms / 1000 - elapsed)
        return ScheduleContext(
            now=now,
            pending_requests=list(self._pending),
            generated_buffer_sec=buffer_sec,
            inflight_generations=self._inflight,
            backpressure=False,  # TODO(M2): 백엔드 backpressure 이벤트 반영
            order_position=self.order.position,
            next_slot=self.order.peek(),
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
            if decision.send_ack:
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
                    await self._transition(req, RequestState.GENERATED)
                await self._publish(sub)
        except Exception:
            # 생성 태스크는 아무도 await하지 않으므로 여기서 기록하지 않으면 실패가 사라진다
            # (실 TTS API의 403·타임아웃 등). TODO(M2): 1회 재시도 후 filler 대체, 요청 상태 복구.
            logger.exception("GENERATION_FAILED %s %s", job.job_id, job.corner_type)
        finally:
            self._inflight -= 1

    async def _transition(self, req: ListenerRequest, state: RequestState) -> None:
        req.state = state
        self.telemetry.log_request_state(req.request_id, state)
        await self.transport.notify_request_state(req.request_id, state)

    async def _publish(self, sub: SegmentSubmission) -> None:
        self._produced_ms += sub.duration_ms
        self._submitted += 1
        await self.transport.publish_segment(sub)
