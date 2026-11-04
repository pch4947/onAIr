"""테스트 공통 — 뉴스 형식 방송이 실제 피드에 접속하지 않게 한다."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_real_feeds(monkeypatch):
    monkeypatch.setattr("onair_engine.sources.rss.fetch_feed",
                        lambda url: b"<rss><channel></channel></rss>")
