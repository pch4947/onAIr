"""RSS 수집 — 파싱, 정리, 캐시, 분야 분리 (ENGINE_ARCHITECTURE 4.7절)."""
from __future__ import annotations

import asyncio
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from onair_engine.corners.run import MaterialSources, TrackSource
from onair_engine.domain import StationConfig, StationProfile
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.sources.rss import (
    Feed,
    FeedItem,
    RssCollector,
    clean_text,
    fetch_feed,
    load_news_feeds,
    parse_feed,
)

CONFIG = Path(__file__).resolve().parents[1] / "config"
FEED = Feed("테스트일보", "https://feed.example.com/rss", "IT")


def _rss(*items: tuple[str, str, float | None]) -> bytes:
    body = "".join(
        f"<item><title><![CDATA[{title}]]></title><link>https://a.example.com/{i}</link>"
        f"<description><![CDATA[{summary}]]></description>"
        + (f"<pubDate>{formatdate(at)}</pubDate>" if at else "") + "</item>"
        for i, (title, summary, at) in enumerate(items)
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>{body}</channel></rss>'.encode()


def test_parse_rss2_item():
    now = time.time()
    [item] = parse_feed(_rss(("기사 제목", "기사 요약", now)), FEED)
    assert (item.title, item.summary) == ("기사 제목", "기사 요약")
    assert (item.source, item.section) == ("테스트일보", "IT")
    assert item.source_url == "https://a.example.com/0"
    assert item.published_at == pytest.approx(now, abs=1)


def test_parse_atom_entry_and_iso_date():
    raw = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <title>아톰 기사</title><link href="https://a.example.com/atom"/>
      <summary type="html">&lt;p&gt;아톰 &lt;b&gt;요약&lt;/b&gt;&lt;/p&gt;</summary>
      <published>2026-11-04T10:00:00+09:00</published></entry></feed>""".encode()
    [item] = parse_feed(raw, FEED)
    assert item.source_url == "https://a.example.com/atom"
    assert item.summary == "아톰 요약"
    assert item.published_at > 0


def test_parse_keeps_title_only_item_and_skips_untitled():
    raw = (b"<rss><channel><item><title>\xec\xa0\x9c\xeb\xaa\xa9</title></item>"
           b"<item><description>x</description></item></channel></rss>")
    [item] = parse_feed(raw, FEED)
    assert item.summary == "제목"


@pytest.mark.parametrize("raw", [
    b'<!DOCTYPE lolz [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>',
    b'<!DOCTYPE html><html><body>not found</body></html>',
])
def test_parse_rejects_dtd(raw):
    with pytest.raises(ValueError):
        parse_feed(raw, FEED)


@pytest.mark.parametrize(("raw", "expected"), [
    ("(제주=OO뉴스) 홍길동 기자 = 지난 4월 일이다", "지난 4월 일이다"),
    ("홍길동 기자 = 본문", "본문"),
    ("[테스트일보]본문입니다", "본문입니다"),
    ('<img src="x.jpg"/>본문 <a href="y">링크</a>', "본문 링크"),
    ("본문 https://a.example.com/1 끝 reporter@example.com", "본문 끝"),
    ("<script>alert(1)</script>본문", "본문"),
    ("A &amp; B", "A & B"),
])
def test_clean_text(raw, expected):
    assert clean_text(raw) == expected


def _collector(pages: dict[str, bytes | Exception], **kwargs) -> RssCollector:
    def fetch(url: str) -> bytes:
        page = pages[url]
        if isinstance(page, Exception):
            raise page
        return page

    feeds = [Feed("매체", url, section) for url, section in kwargs.pop("feeds")]
    return RssCollector(feeds=feeds, fetch=fetch, **kwargs)


def test_refresh_sorts_newest_first_dedups_and_drops_stale():
    now = time.time()
    pages = {"u1": _rss(("오래된", "s", now - 90000), ("이전", "s", now - 100), ("최신", "s", now)),
             "u2": _rss(("최신", "다른 매체의 같은 제목", now), ("날짜없음", "s", None))}
    rss = _collector(pages, feeds=[("u1", "IT"), ("u2", "IT")], max_age_sec=86400)
    assert asyncio.run(rss.refresh()) == 3
    assert [it.title for it in rss.latest(10, "IT")] == ["최신", "이전", "날짜없음"]


def test_latest_separates_sections_and_mixes_for_headline():
    now = time.time()
    pages = {"u1": _rss(("IT1", "s", now), ("IT2", "s", now - 1), ("IT3", "s", now - 2)),
             "u2": _rss(("경제1", "s", now - 10))}
    rss = _collector(pages, feeds=[("u1", "IT"), ("u2", "경제")])
    asyncio.run(rss.refresh())
    assert [it.title for it in rss.latest(10, "경제")] == ["경제1"]
    assert [it.title for it in rss.latest(2, "IT")] == ["IT1", "IT2"]
    assert [it.title for it in rss.latest(3)] == ["IT1", "경제1", "IT2"]  # 헤드라인은 분야를 섞는다


def test_failed_feed_keeps_previous_items_and_others_update():
    now = time.time()
    pages = {"u1": _rss(("유지", "s", now)), "u2": _rss(("이전", "s", now))}
    rss = _collector(pages, feeds=[("u1", "IT"), ("u2", "경제")])
    asyncio.run(rss.refresh())
    pages["u1"] = OSError("timeout")
    pages["u2"] = _rss(("새 기사", "s", now))
    assert asyncio.run(rss.refresh()) == 2
    assert [it.title for it in rss.latest(1, "IT")] == ["유지"]
    assert [it.title for it in rss.latest(1, "경제")] == ["새 기사"]


def test_malformed_feed_does_not_stop_others():
    now = time.time()
    rss = _collector({"bad": b"<not-xml", "ok": _rss(("정상", "s", now))},
                     feeds=[("bad", "IT"), ("ok", "IT")])
    assert asyncio.run(rss.refresh()) == 1


def test_l0_violation_is_not_cached():
    now = time.time()
    rss = _collector({"u": _rss(("금칙어 기사", "s", now), ("정상", "s", now))},
                     feeds=[("u", "IT")],
                     violates=lambda text: "금칙어" if "금칙어" in text else None)
    asyncio.run(rss.refresh())
    assert [it.title for it in rss.latest(10, "IT")] == ["정상"]


def test_run_sets_ready_even_when_all_feeds_fail():
    rss = _collector({"u": OSError("down")}, feeds=[("u", "IT")])

    async def scenario():
        task = asyncio.create_task(rss.run())
        await asyncio.wait_for(rss.ready.wait(), 5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert rss.latest(10) == []


def test_run_polls_again():
    calls = []

    def fetch(url):
        calls.append(url)
        return _rss()

    rss = RssCollector(feeds=[Feed("매체", "u")], fetch=fetch, poll_sec=0.01)

    async def scenario():
        task = asyncio.create_task(rss.run())
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(calls) >= 2


def test_fetch_feed_sends_user_agent_and_reads_body():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["agent"] = self.headers["User-Agent"]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(_rss(("기사", "요약", None)))

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        raw = fetch_feed(f"http://127.0.0.1:{server.server_port}/rss")
    finally:
        server.shutdown()
    assert parse_feed(raw, FEED)[0].title == "기사"
    assert seen["agent"].startswith("onAIr-engine")


# ── 코너 연결 ─────────────────────────────────────────────────────────

def test_sources_take_only_requested_section_and_name_source():
    rss = RssCollector([
        FeedItem("IT 기사", "요약", "https://x.example.com/1", source="테스트일보", section="IT"),
        FeedItem("경제 기사", "요약", "https://www.y.example.com/1", section="경제"),
    ])
    sources = MaterialSources(tracks=TrackSource(), rss=rss)
    assert sources.count("news", "스포츠") == 0
    assert sources.take("news", 3, "IT") == [
        {"title": "IT 기사", "summary": "요약", "source": "테스트일보"}]
    assert sources.take("news", 3, "경제")[0]["source"] == "y.example.com"  # 매체 이름이 없으면 호스트명
    assert sources.count("news") == 0  # 전한 기사는 분야와 상관없이 다시 꺼내지 않는다


# ── 스테이션 조립 ─────────────────────────────────────────────────────

def _engine(tmp_path, fmt: str, sections=None) -> StationEngine:
    config = StationConfig(
        station_id=f"st_{fmt}", profile=StationProfile(dj_name="새벽", tone="차분", concept="밤"),
        broadcast_minutes=60, format=fmt, news_sections=sections,
    )
    settings = EngineSettings(
        audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "m.sqlite",
        safety_rules_path=CONFIG / "safety_rules.yaml", chat_rules_path=CONFIG / "chat_rules.yaml",
        news_feeds_path=CONFIG / "news_feeds.yaml", formats_dir=CONFIG / "formats",
    )

    class NoTransport:
        pass

    return StationEngine(config, settings, NoTransport())


def test_news_station_polls_only_selected_sections(tmp_path):
    engine = _engine(tmp_path, "news", ["it", "sports"])
    assert {f.section for f in engine.rss.feeds} == {"IT", "스포츠"}
    engine.telemetry.close()


def test_other_formats_do_not_poll(tmp_path):
    engine = _engine(tmp_path, "talk")
    assert engine.rss.feeds == []
    engine.telemetry.close()


def test_every_section_in_format_has_a_feed():
    feeds = load_news_feeds(CONFIG / "news_feeds.yaml")
    assert set(feeds.feeds) == {"general", "economy", "it", "sports", "culture"}
    assert all(feeds.feeds.values())
