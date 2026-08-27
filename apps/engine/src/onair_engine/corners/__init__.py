"""MVP 임시 코너 세트 (확인 2 결정) — 기획서 4.2 확정 시 교체/추가."""
from __future__ import annotations

from ..catalog import Catalog
from ..sources.rss import RssCollector
from .base import L1_GUARD, Corner, system_prompt
from .briefing import BriefingCorner
from .filler import FillerCorner
from .music_intro import MusicIntroCorner
from .opening import OpeningCorner
from .request_reply import RequestReplyCorner

__all__ = ["L1_GUARD", "Corner", "build_corners", "system_prompt"]


def build_corners(*, catalog: Catalog, rss: RssCollector) -> dict[str, Corner]:
    corners: list[Corner] = [
        OpeningCorner(),
        BriefingCorner(rss),
        MusicIntroCorner(catalog),
        RequestReplyCorner(),
        FillerCorner(),
    ]
    return {c.corner_type: c for c in corners}
