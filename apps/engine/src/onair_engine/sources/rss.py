"""소스 수집기 — 주기 폴링 + 로컬 캐시 (설계 문서 4.7, F-04).

생성 경로는 캐시만 읽는다 — 대본 생성 중에 외부 네트워크를 기다리지 않는다.
캐시가 비면 브리핑 코너는 스킵되고 스케줄러가 filler로 대체한다.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FeedItem:
    title: str
    summary: str
    source_url: str  # 브리핑 대본의 소스 인용 강제용 (환각 리스크 대응)


class RssCollector:
    """MVP 스텁. TODO(M2): asyncio 백그라운드 태스크로 주기 폴링·캐시 적재."""

    def __init__(self):
        self._cache: list[FeedItem] = []

    def latest(self, n: int) -> list[FeedItem]:
        return self._cache[:n]
