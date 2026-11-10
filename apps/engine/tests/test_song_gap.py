"""곡 연속 방지 — 사이 코너가 다 빠지면 다음 곡도 건너뛴다. 연달아서는 두 번까지 (#228)."""
import asyncio
import sqlite3
from itertools import pairwise

from test_scheduler import FakePipeline, _engine


def _corners(tmp_path, max_segments=30, before=None, **config) -> list[tuple[str, str]]:
    engine, _ = _engine(tmp_path, **config)
    engine.scheduler.pipeline = FakePipeline()
    if before:
        before(engine)
    asyncio.run(engine.run(max_segments=max_segments))
    db = sqlite3.connect(tmp_path / "metrics.sqlite")
    return db.execute("SELECT corner_type, event FROM corner_log WHERE event IN ('enter', 'hold') "
                      "ORDER BY rowid").fetchall()


def _entered(rows):
    return [corner for corner, event in rows if event == "enter"]


def test_skipped_text_corner_also_skips_the_song_after_it(tmp_path):
    # 문자가 없다 — text_corner가 빠진다. 전에는 [곡 소개, 곡, 곡 소개, 곡]이 이어졌다
    rows = _corners(tmp_path, corners=["topic_talk"])
    entered = _entered(rows)
    assert ("music_intro", "hold") in rows
    assert all(not (a == b == "music_intro") for a, b in pairwise(entered)), entered
    assert entered[:5] == ["opening", "topic_talk", "music_intro", "topic_talk", "music_intro"]


def test_main_corners_that_skip_in_a_row_do_not_leave_songs_back_to_back(tmp_path):
    # quiet 시나리오 구성 — 본 코너에 문자 소개가 있다. 빠짐 → 곡 → 빠짐 → 곡이 되면 곡이 이어진다
    rows = _corners(tmp_path, max_segments=40, corners=["topic_talk", "text_corner", "story_corner"])
    entered = _entered(rows)
    assert entered.count("topic_talk") >= 3
    assert all(not (a == b == "music_intro") for a, b in pairwise(entered)), entered


def test_songs_still_play_when_every_talk_corner_is_skipped(tmp_path):
    # 본 코너가 문자 소개뿐이고 문자가 없다 — 곡까지 막으면 무음이다. 연달아서는 두 번까지만 건너뛴다
    entered = _entered(_corners(tmp_path, max_segments=8, corners=["text_corner"]))
    assert entered.count("music_intro") >= 3


def test_requested_song_is_not_held(tmp_path):
    def queue_request(engine):
        engine.scheduler._talk_since_song = False  # 방금 곡이 나갔다
        engine.on_track_queued({"track": {"track_ref": "yt:req", "title": "신청곡", "artist": "가수",
                                          "duration_ms": 1000}, "source": "request", "request_id": "rq"})

    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    queue_request(engine)
    music = engine.scheduler.fmt.corner("music_intro")
    assert not engine.scheduler._holds_song(music), "신청자가 기다린다"
    engine.sources.tracks.take()
    assert engine.scheduler._holds_song(music)


def test_music_format_keeps_songs_back_to_back(tmp_path):
    rows = _corners(tmp_path, max_segments=6, format="music")
    entered = _entered(rows)
    assert ("music_intro", "hold") not in rows
    assert entered[:3] == ["opening", "music_intro", "music_intro"]
