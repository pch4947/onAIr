from __future__ import annotations

from ..domain import Material, Prompt, ScheduleContext, StationProfile
from ..prompt_data import STORY, data_block
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
            # 사연은 지시문에 이어붙이지 않는다 — 사연 속 문장이 지시로 읽히지 않게 (프롬프트 인젝션)
            user=("청취자 사연에 답하는 멘트를 3문장 이내로 작성하라. 사연은 아래 데이터 블록에 있다.\n"
                  + data_block(STORY, req.body)),
        )
