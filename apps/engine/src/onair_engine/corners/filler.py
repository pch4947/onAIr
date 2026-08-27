from __future__ import annotations

from ..domain import Material, Prompt, ScheduleContext, StationProfile
from .base import system_prompt


class FillerCorner:
    """버퍼 방어용 짧은 브릿지 멘트 — 백프레셔 대응과 코너 대체의 기본 수단."""

    corner_type = "filler"
    expected_duration_sec = (5, 15)

    async def gather(self, ctx: ScheduleContext) -> Material:
        return Material()

    def build_prompt(self, material: Material, profile: StationProfile) -> Prompt:
        return Prompt(
            system=system_prompt(profile),
            user="곡 사이를 잇는 짧은 브릿지 멘트를 1~2문장으로 작성하라.",
        )
