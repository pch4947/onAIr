from __future__ import annotations

from ..domain import Material, Prompt, ScheduleContext, StationProfile
from ..sources.rss import RssCollector
from .base import system_prompt


class BriefingCorner:
    """RSS 수집 소재 기반 브리핑 (F-04). 캐시가 비면 스킵된다 — 설계 문서 4.7."""

    corner_type = "briefing"
    expected_duration_sec = (20, 60)

    def __init__(self, rss: RssCollector):
        self._rss = rss

    async def gather(self, ctx: ScheduleContext) -> Material | None:
        items = self._rss.latest(3)
        if not items:
            return None
        text = "\n".join(f"- {it.title}: {it.summary} (출처: {it.source_url})" for it in items)
        return Material(text=text)

    def build_prompt(self, material: Material, profile: StationProfile) -> Prompt:
        return Prompt(
            system=system_prompt(profile),
            user=(
                "다음 수집 소재만 근거로 짧은 브리핑 멘트를 작성하라. "
                "소재에 없는 사실은 언급하지 않고, 출처를 밝힌다.\n" + material.text
            ),
        )
