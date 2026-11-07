"""소스 수집기 — 주기 폴링 + 메모리 캐시 (설계 문서 4.7, F-04).

생성 경로는 캐시만 읽는다 — 대본 생성 중에 외부 네트워크를 기다리지 않는다.
캐시가 비면 뉴스 코너는 진입 조건에서 걸러져 건너뛴다.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

USER_AGENT = "onAIr-engine/0.1"  # urllib 기본값(Python-urllib/3.x)은 막는 서버가 있다
FETCH_TIMEOUT_SEC = 10.0
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_ITEMS_PER_FEED = 30
MAX_SUMMARY_CHARS = 300

_DTD = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_HIDDEN_TAG = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_URL = re.compile(r"https?://\S+")
_EMAIL = re.compile(r"\S+@\S+\.\S+")
# "(제주=OO뉴스) 홍길동 기자 = ", "[OO일보]" — 소리 내어 읽을 내용이 아니다
_DATELINE = re.compile(r"^(?:\[[^\]]{1,20}\]\s*)?(?:[(\[][^)\]]*=[^)\]]*[)\]]\s*)?"
                       r"(?:[^=\n]{0,30}기자\s*=\s*)?")


@dataclass
class FeedItem:
    title: str
    summary: str
    source_url: str
    source: str | None = None  # 대본에서 출처로 말할 매체 이름. 없으면 source_url의 호스트명
    section: str | None = None  # 분야 이름 (ClockEntry.label과 같은 값). None이면 모든 분야에 쓴다
    published_at: float = 0.0  # 0이면 피드에 발행 시각이 없다


@dataclass(frozen=True)
class Feed:
    source: str
    url: str
    section: str | None = None


class FeedEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1)
    url: str = Field(pattern=r"^https?://")


class NewsFeeds(BaseModel):
    """config/news_feeds.yaml — feeds의 키는 분야 코드(general·it …)다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    poll_sec: float = Field(default=600.0, gt=0)
    max_age_hours: float = Field(default=24.0, gt=0)
    feeds: dict[str, list[FeedEntry]] = Field(default_factory=dict)


def load_news_feeds(path: str | Path) -> NewsFeeds:
    with open(path, encoding="utf-8") as f:
        return NewsFeeds.model_validate(yaml.safe_load(f))


def source_name(url: str) -> str:
    """피드에 매체 이름이 없을 때 출처로 말할 이름 — URL 전체가 아니라 호스트명."""
    host = urlparse(url).hostname or url
    return host.removeprefix("www.")


def clean_text(raw: str | None) -> str:
    """피드 본문 → 말할 수 있는 한 줄. HTML, URL, 이메일, 기자 표기를 뺀다."""
    text = html.unescape(raw or "")
    text = _TAG.sub(" ", _HIDDEN_TAG.sub(" ", text))
    text = " ".join(_EMAIL.sub(" ", _URL.sub(" ", text)).split())
    return _DATELINE.sub("", text, count=1).strip()


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _parse_time(raw: str | None) -> float:
    if not raw:
        return 0.0
    raw = raw.strip()
    try:
        parsed = parsedate_to_datetime(raw)  # RSS 2.0 pubDate
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(raw)  # Atom, dc:date
        except ValueError:
            return 0.0
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()


def _item_link(fields: dict[str, ET.Element]) -> str:
    link = fields.get("link")
    if link is None:
        return ""
    return (link.text or link.get("href") or "").strip()


def parse_feed(raw: bytes, feed: Feed) -> list[FeedItem]:
    """RSS 1.0·2.0, Atom → 기사 목록. 제목이 없는 항목은 버리고, 요약이 없으면 제목을 쓴다."""
    if _DTD.search(raw):  # 피드에 DTD가 필요하지 않다 — 엔티티 확장을 막고 HTML 오류 페이지도 거른다
        raise ValueError("DTD가 있는 문서는 피드로 읽지 않는다")
    items = []
    for node in ET.fromstring(raw).iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        fields: dict[str, ET.Element] = {}
        for child in node:
            fields.setdefault(_local(child.tag), child)
        title = clean_text(_text(fields, "title"))
        if not title:
            continue
        summary = clean_text(_text(fields, "description", "summary", "content", "encoded"))
        items.append(FeedItem(
            title=title, summary=(summary or title)[:MAX_SUMMARY_CHARS],
            source_url=_item_link(fields) or feed.url, source=feed.source, section=feed.section,
            published_at=_parse_time(_text(fields, "pubDate", "published", "updated", "date")),
        ))
        if len(items) >= MAX_ITEMS_PER_FEED:
            break
    return items


def _text(fields: dict[str, ET.Element], *names: str) -> str | None:
    for name in names:
        if name in fields and fields[name].text:
            return fields[name].text
    return None


def fetch_feed(url: str, timeout: float = FETCH_TIMEOUT_SEC) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(MAX_FEED_BYTES + 1)
    if len(raw) > MAX_FEED_BYTES:
        raise ValueError(f"피드가 {MAX_FEED_BYTES}바이트를 넘는다")
    return raw


class RssCollector:
    """스테이션 하나의 뉴스 소재. run()이 폴링하고 코너는 latest()로 캐시만 읽는다.

    items는 평가·테스트용 고정 소재다. 실패한 피드는 직전 결과를 유지한다.
    """

    def __init__(self, items: list[FeedItem] | None = None, *, feeds: Iterable[Feed] = (),
                 poll_sec: float = 600.0, max_age_sec: float = 86400.0,
                 violates: Callable[[str], object] | None = None,
                 fetch: Callable[[str], bytes] | None = None,
                 on_refresh: Callable[[dict[str, int], int], None] | None = None):
        self.feeds = list(feeds)
        self._on_refresh = on_refresh  # (분야별 기사 수, 실패한 피드 수) — 엔진 패널 이벤트
        self.ready = asyncio.Event()  # 첫 수집이 끝났다 (성공 여부와 무관)
        self._cache: list[FeedItem] = list(items or [])
        self._fixed = list(items or [])
        self._by_url: dict[str, list[FeedItem]] = {}
        self._poll_sec = poll_sec
        self._max_age_sec = max_age_sec
        self._violates = violates  # L0 검사 — 피드도 외부 텍스트다
        self._fetch = fetch or fetch_feed

    def latest(self, n: int, section: str | None = None) -> list[FeedItem]:
        """section을 주면 그 분야만, 안 주면(헤드라인) 분야를 번갈아 가며 최신순으로."""
        if section is not None:
            return [it for it in self._cache if it.section in (section, None)][:n]
        groups: dict[str | None, list[FeedItem]] = {}
        for it in self._cache:
            groups.setdefault(it.section, []).append(it)
        mixed: list[FeedItem] = []
        rank = 0
        while len(mixed) < n and any(rank < len(g) for g in groups.values()):
            mixed += [g[rank] for g in groups.values() if rank < len(g)]
            rank += 1
        return mixed[:n]

    async def refresh(self) -> int:
        """모든 피드를 한 번 읽어 캐시를 바꾼다. 캐시의 기사 수를 돌려준다."""
        results = await asyncio.gather(*(self._read(f) for f in self.feeds),
                                       return_exceptions=True)
        failed = 0
        for feed, result in zip(self.feeds, results):
            if isinstance(result, BaseException):
                failed += 1
                logger.warning("RSS 수집 실패 %s: %r", feed.url, result)
            else:
                self._by_url[feed.url] = result
        self._cache = self._fixed + self._rebuild()
        if self._on_refresh is not None:
            counts: dict[str, int] = {}
            for it in self._cache:
                counts[it.section or "기타"] = counts.get(it.section or "기타", 0) + 1
            self._on_refresh(counts, failed)
        return len(self._cache)

    async def run(self) -> None:
        """첫 수집 후 주기 폴링. 방송이 끝날 때 태스크를 취소한다."""
        try:
            await self.refresh()
        finally:
            self.ready.set()
        while True:
            await asyncio.sleep(self._poll_sec)
            await self.refresh()

    async def _read(self, feed: Feed) -> list[FeedItem]:
        # 스레드는 취소되지 않으므로 urlopen 제한 시간을 넘겨 늘어지면 여기서 끊는다
        raw = await asyncio.wait_for(asyncio.to_thread(self._fetch, feed.url),
                                     FETCH_TIMEOUT_SEC * 2)
        return parse_feed(raw, feed)

    def _rebuild(self) -> list[FeedItem]:
        oldest = time.time() - self._max_age_sec
        seen: set[str] = set()
        kept = []
        for feed in self.feeds:  # 같은 URL을 여러 분야에 적어도 항목은 분야별로 만들어져 있다
            for it in self._by_url.get(feed.url, []):
                if it.title in seen or 0 < it.published_at < oldest:
                    continue
                seen.add(it.title)
                if self._violates and self._violates(f"{it.title}\n{it.summary}") is not None:
                    continue
                kept.append(it)
        kept.sort(key=lambda it: it.published_at, reverse=True)
        return kept
