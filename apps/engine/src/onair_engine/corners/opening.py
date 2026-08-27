from __future__ import annotations

from ..domain import Material, Prompt, ScheduleContext, StationProfile
from .base import system_prompt


class OpeningCorner:
    corner_type = "opening"
    expected_duration_sec = (15, 40)

    async def gather(self, ctx: ScheduleContext) -> Material:
        return Material()

    def build_prompt(self, material: Material, profile: StationProfile) -> Prompt:
        return Prompt(
            system=system_prompt(profile),
            user="방송을 시작한다. 스테이션 컨셉에 맞는 오프닝 멘트를 3문장 이내로 작성하라.",
        )
