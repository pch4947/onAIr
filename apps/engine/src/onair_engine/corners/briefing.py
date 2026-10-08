from __future__ import annotations

from ..domain import Material, Prompt, ScheduleContext, StationProfile
from ..prompt_data import SOURCES, data_block
from ..sources.rss import RssCollector, source_name
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
        # URL은 소리 내어 읽을 수 없고 L2 룰(URL 금지)에 걸린다 — 매체 이름만 넘긴다
        text = "\n".join(f"- {it.title}: {it.summary} (출처: {source_name(it.source_url)})"
                         for it in items)
        return Material(text=text)

    def build_prompt(self, material: Material, profile: StationProfile) -> Prompt:
        return Prompt(
            system=system_prompt(profile),
            user=(
                "다음 수집 소재만 근거로 짧은 브리핑 멘트를 작성하라. "
                "소재에 없는 사실은 언급하지 않고, 출처는 매체 이름으로 밝힌다.\n"
                # 피드 제목·요약은 외부 텍스트다 — 사연과 같이 데이터로 넣는다
                + data_block(SOURCES, material.text)
            ),
        )
