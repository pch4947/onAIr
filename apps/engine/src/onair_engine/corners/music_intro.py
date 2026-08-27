from __future__ import annotations

from ..catalog import Catalog
from ..domain import Material, Prompt, ScheduleContext, StationProfile
from .base import system_prompt


class MusicIntroCorner:
    """곡 소개 멘트 생성. 곡 재생·믹싱은 백엔드 담당 — music_ref로 곡을 지정한다."""

    corner_type = "music_intro"
    expected_duration_sec = (10, 25)

    def __init__(self, catalog: Catalog):
        self._catalog = catalog

    async def gather(self, ctx: ScheduleContext) -> Material | None:
        track = self._catalog.pick()
        if track is None:
            return None
        return Material(
            track_id=track.track_id,
            extra={"title": track.title, "artist": track.artist, "mood": track.mood},
        )

    def build_prompt(self, material: Material, profile: StationProfile) -> Prompt:
        info = material.extra
        return Prompt(
            system=system_prompt(profile),
            user=(
                "다음 곡을 소개하는 멘트를 2문장 이내로 작성하라. "
                f"곡: {info['title']} — {info['artist']} (무드: {info['mood']})"
            ),
        )
