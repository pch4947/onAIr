from __future__ import annotations

from ..domain import Material, Prompt, ScheduleContext, StationProfile
from .base import system_prompt


class RequestReplyCorner:
    """청취자 요청에 대한 본 답변. 접수 확인(ack)은 별도 단축 경로(ack.py)."""

    corner_type = "request_reply"
    expected_duration_sec = (15, 45)

    async def gather(self, ctx: ScheduleContext) -> Material | None:
        # 소재는 요청 자체 — 스케줄러가 Material(request=...)로 직접 구성한다
        return None

    def build_prompt(self, material: Material, profile: StationProfile) -> Prompt:
        req = material.request
        assert req is not None
        return Prompt(
            system=system_prompt(profile),
            user=f"청취자 사연에 답하는 멘트를 3문장 이내로 작성하라. 사연: {req.body}",
        )
