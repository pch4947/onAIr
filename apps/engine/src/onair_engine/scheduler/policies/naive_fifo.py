"""MVP 정책 — 정책 A/B/C(기획서 8장 확정 후)의 자리 표시자."""
from __future__ import annotations

from ...domain import Decision, ScheduleContext


class NaiveFifoPolicy:
    """코너 진행 우선, 요청은 FIFO 즉시 반영, ack는 항상 송출."""

    name = "naive_fifo"

    def __init__(self, target_buffer_sec: float = 30.0, max_inflight: int = 2):
        self.target_buffer_sec = target_buffer_sec
        self.max_inflight = max_inflight

    def decide(self, ctx: ScheduleContext) -> Decision:
        if ctx.inflight_generations >= self.max_inflight:
            return Decision(action="wait")
        if ctx.pending_requests:
            return Decision(action="generate", request=ctx.pending_requests[0], send_ack=True)
        if ctx.generated_buffer_sec >= self.target_buffer_sec:
            return Decision(action="wait")
        return Decision(action="generate", corner_type=ctx.next_slot)
