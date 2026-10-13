"""형식 · 클락 · 코너 진행 (docs/CORNER_DESIGN.md 3·6·7절) — 설정 파일 검증과 순수 로직."""
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from onair_engine.corners.run import CornerRun, MaterialSources, Thread, TrackSource
from onair_engine.format import FORMATS, Clock, ClockEntry, FormatSpec, load_format
from onair_engine.sources.rss import FeedItem, RssCollector

FORMATS_DIR = Path(__file__).resolve().parent.parent / "config" / "formats"


def _fmt(name: str) -> FormatSpec:
    return load_format(name, FORMATS_DIR)


def test_all_formats_load():
    for name in FORMATS:
        fmt = _fmt(name)
        assert fmt.format == name
        assert {"low", "mid", "high"} <= set(fmt.intensity)


def test_news_closes_every_listener_input():
    # 뉴스 형식은 청취자 입력을 방송에 반영하지 않는다 (D3)
    fmt = _fmt("news")
    for corner in fmt.corners.values():
        if corner.corner_type == "filler":
            continue
        assert corner.mode("chat") in ("closed", "defer"), corner.corner_type
        assert corner.subjectivity == "none", corner.corner_type


def test_music_does_not_read_messages():
    # 음악 형식은 사연 보내기를 받지 않는다 (CORNER_DESIGN U4) — READ 코너가 없다
    fmt = _fmt("music")
    assert all(c.mode("message") != "read" for c in fmt.corners.values())


def _spec(**override) -> dict:
    base = _fmt("talk").model_dump()
    return {**base, **override}


@pytest.mark.parametrize("override, message", [
    ({"cycle": ["weather"]}, "정의되지 않은 코너"),
    ({"cycle": ["$oops"]}, "알 수 없는 자리 표시"),
    ({"default_main": ["weather"]}, "정의되지 않은 코너"),
])
def test_format_spec_rejects_bad_cycle(override, message):
    with pytest.raises(ValidationError, match=message):
        FormatSpec.model_validate(_spec(**override))


def test_loader_rejects_unknown_step_kind(tmp_path):
    shutil.copytree(FORMATS_DIR, tmp_path / "formats")
    path = tmp_path / "formats" / "music.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("kind: artist_talk",
                                                             "kind: weather_report"),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="weather_report"):
        load_format("music", tmp_path / "formats")


# ── 클락 ─────────────────────────────────────────────────────────────

def _names(clock: Clock, n: int, remaining: float = 3600) -> list[str]:
    out = []
    for _ in range(n):
        entry = clock.next(remaining)
        out.append(entry.corner.corner_type + (f"/{entry.label}" if entry.label else ""))
    return out


def test_clock_opens_then_alternates_host_corners():
    clock = Clock(_fmt("talk"), main_corners=["topic_talk", "story_corner"])
    assert _names(clock, 8) == ["opening", "topic_talk", "music_intro", "text_corner",
                                "music_intro", "story_corner", "music_intro", "topic_talk"]


def test_clock_uses_format_defaults():
    assert Clock(_fmt("talk")).main_titles() == ["사연 읽는 시간", "오늘의 이야기"]


def test_clock_rejects_corner_not_offered_to_host():
    with pytest.raises(ValueError, match="고를 수 없는"):
        Clock(_fmt("talk"), main_corners=["music_intro"])


def test_clock_expands_news_sections_with_korean_names():
    clock = Clock(_fmt("news"), sections=["it", "sports"])
    assert _names(clock, 6) == ["opening", "headline", "section/IT", "section/스포츠",
                                "recap", "music_intro"]


def test_clock_goes_to_closing_when_time_runs_out():
    clock = Clock(_fmt("music"))
    clock.next(3600)  # opening
    clock.next(3600)
    closing = clock.next(clock.closing_sec())  # 남은 시간이 엔딩 길이에 닿았다
    assert closing.corner.corner_type == "closing"
    assert clock.next(0) is None and clock.phase == "done"


# ── 코너 진행 ─────────────────────────────────────────────────────────

def _sources(news: int = 0) -> MaterialSources:
    items = [FeedItem(f"기사 {i}", "요약", f"https://news{i}.example.com/1") for i in range(news)]
    return MaterialSources(tracks=TrackSource(), rss=RssCollector(items))


def _run(fmt: FormatSpec, corner: str, sources: MaterialSources, intensity="mid") -> CornerRun:
    return CornerRun(ClockEntry(fmt.corner(corner)), sources=sources,
                     intensity=fmt.intensity[intensity], thread=Thread(), started_ms=0)


def test_corner_runs_steps_in_order_with_repeats():
    fmt = _fmt("talk")
    run = _run(fmt, "topic_talk", _sources())
    kinds = []
    while (step := run.next_step(produced_ms=0)) is not None:
        kinds.append(step.step.glue or step.step.kind)
    assert kinds == ["call", "talk_open", "talk", "talk", "talk"]


def test_corner_over_budget_skips_to_close_glue():
    fmt = _fmt("talk")
    sources = _sources()
    sources.board_stories += [{"nickname": "a", "body": "사연 1"}, {"nickname": "b", "body": "사연 2"}]
    run = _run(fmt, "story_corner", sources)
    assert run.next_step(0).step.glue == "call"
    assert run.next_step(0).step.kind == "story"
    over = (fmt.corner("story_corner").budget_sec[1] + 1) * 1000
    assert run.next_step(over).step.glue == "close"  # 두 번째 사연은 건너뛰고 마무리 문구
    assert run.next_step(over) is None
    assert len(sources.board_stories) == 1  # 꺼내지 않은 사연은 다음 차례에 남는다


def test_repeat_follows_intensity_preset():
    fmt = _fmt("talk")
    sources = _sources()
    sources.messages += [{"nickname": f"n{i}", "body": f"문자 {i}"} for i in range(10)]
    for level, count in (("low", 3), ("high", 5)):
        run = _run(fmt, "text_corner", sources, intensity=level)
        steps = []
        while (s := run.next_step(0)) is not None:
            steps.append(s.step.kind)
        assert steps.count("text_read") == count


def test_news_items_are_not_repeated_and_feed_recap():
    fmt = _fmt("news")
    sources = _sources(news=4)
    headline = _run(fmt, "headline", sources).next_step(0)
    assert len(headline.items) == 3
    section = _run(fmt, "section", sources)
    titles = [s.items[0]["title"] for s in iter(lambda: section.next_step(0), None)]
    assert titles == ["기사 3"]  # 헤드라인에서 전한 기사는 다시 꺼내지 않는다
    assert sources.covered_news == ["기사 0", "기사 1", "기사 2", "기사 3"]
    assert sources.satisfies(fmt.corner("recap"))


def test_requires_blocks_corner_without_material():
    fmt = _fmt("talk")
    sources = _sources()
    assert not sources.satisfies(fmt.corner("story_corner"))  # 게시판 사연은 M4부터
    assert not sources.satisfies(fmt.corner("text_corner"))
    assert sources.satisfies(fmt.corner("topic_talk"))


def test_track_source_prefers_backend_queued_track():
    tracks = TrackSource()
    peeked = tracks.peek()
    assert tracks.take() == peeked  # artist_talk이 미리 본 곡을 그대로 소개한다
    tracks.queue({"track_ref": "yt:abc", "title": "밤편지", "artist": "아이유", "duration_ms": 1})
    assert tracks.take()["track_ref"] == "yt:abc"
    dummy = tracks.take()
    assert dummy["track_ref"].startswith("cat_dummy") and dummy["dummy"]  # 다음 track.queued 전까지는 더미
