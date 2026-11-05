"""집계 언급·예고 — 트리거, 횟수 상한, 계측 (CORNER_DESIGN 5.7, 9.2절)."""
import asyncio
import itertools
import sqlite3
import time
from pathlib import Path

import pytest

from onair_engine.corners.steps import AGGREGATE_RULE, PREVIEW_RULE, build_prompt
from onair_engine.domain import ListenerRequest, Material, StationConfig, StationProfile
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.format import Clock, load_format
from onair_engine.inputs.aggregate import ChatTrend, chat_trend
from onair_engine.inputs.rules import load_chat_rules

ROOT = Path(__file__).resolve().parent.parent
RULES = load_chat_rules(ROOT / "config" / "chat_rules.yaml")
FORMATS = ROOT / "config" / "formats"
PROFILE = StationProfile(dj_name="새벽", tone="차분한 존댓말", concept="테스트 방송")
NOW = 1000.0


def _chat(i, body, *, who=None, at=NOW):
    return ListenerRequest(request_id=f"r{i}", kind="chat", body=body,
                           requester_ref=who or f"u{i}", received_at=at)


def _trend(items):
    return chat_trend(items, now=NOW, window_sec=60, rules=RULES, violates=lambda text: None)


def _flood(n=8, on_topic=4):
    return [_chat(i, f"쇼핑 때문에 큰일이에요 {i}" if i < on_topic else f"각자내용{i}")
            for i in range(n)]


def test_trend_found_when_flooded_on_same_keyword():
    assert _trend(_flood()) == ChatTrend("쇼핑", 4, 8)


@pytest.mark.parametrize(("label", "items"), [
    ("건수 미달", _flood(n=7)),
    ("비중 미달", _flood(on_topic=3)),
    ("창 밖", [_chat(i, f"쇼핑 얘기 {i}", at=NOW - 61) for i in range(8)]),
    ("같은 내용 도배", [_chat(i, "쇼핑 얘기") for i in range(8)]),
    ("한 사람 도배", [_chat(i, f"쇼핑 얘기 {i}", who="u1") for i in range(8)]),
    ("감탄 표현", [_chat(i, f"좋아요 번호{i}") for i in range(8)]),
    ("일반어", [_chat(i, f"노래 번호{i}") for i in range(8)]),
])
def test_no_trend(label, items):
    assert _trend(items) is None, label


def test_chat_failing_safety_rules_is_not_counted():
    items = _flood()
    trend = chat_trend(items, now=NOW, window_sec=60, rules=RULES,
                       violates=lambda text: "쇼핑" if "쇼핑" in text else None)
    assert trend is None


def test_tie_breaks_deterministically():
    items = [_chat(i, f"{'쇼핑' if i % 2 else '여행'} 얘기 번호{i}") for i in range(8)]
    assert _trend(items).keyword == "여행"  # 인원과 길이가 같으면 사전순으로 뒤


def test_predicate_token_is_not_a_topic():
    items = [_chat(i, f"정말 큰일이에요 {i}") for i in range(8)]
    assert _trend(items) is None
    items = [_chat(i, f"시험공부 큰일이에요 {i}") for i in range(8)]
    assert _trend(items).keyword == "시험공부"


def test_clock_upcoming_does_not_change_order():
    fmt = load_format("talk", FORMATS)
    plain, peeking = Clock(fmt), Clock(fmt)
    plain.next(1e9), peeking.next(1e9)  # 오프닝
    order = [plain.next(1e9).corner.corner_type for _ in range(8)]
    seen = []
    for _ in range(8):
        ahead = [e.corner.corner_type for e in peeking.upcoming(2)]
        entry = peeking.next(1e9).corner.corner_type
        assert ahead[0] == entry  # 미리 본 것이 실제로 나온다
        seen.append(entry)
    assert seen == order


def test_prompt_adds_one_notice():
    trend = ChatTrend("쇼핑", 4, 8)
    aggregate = build_prompt("talk", Material(topic="주제", extra={"aggregate": trend}), PROFILE)
    assert AGGREGATE_RULE in aggregate.user and "쇼핑" in aggregate.user
    preview = build_prompt("talk", Material(topic="주제", extra={"preview": {"title": "문자 소개"}}),
                           PROFILE)
    assert "문자 소개" in preview.user and PREVIEW_RULE.split("{")[0] in preview.user
    assert AGGREGATE_RULE not in preview.user


# ── 방송 통합 ─────────────────────────────────────────────────────────

class Silent:
    async def publish_segment(self, sub):
        pass

    async def notify_request_state(self, request_id, state):
        pass

    async def notify_station(self, event_type, payload):
        pass


def _engine(tmp_path, **config) -> StationEngine:
    cfg = StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=30,
                        topic="오늘의 주제", **config)
    settings = EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "m.sqlite",
                              safety_rules_path=ROOT / "config" / "safety_rules.yaml",
                              chat_rules_path=ROOT / "config" / "chat_rules.yaml",
                              formats_dir=FORMATS, target_buffer_sec=1e9)
    return StationEngine(cfg, settings, Silent())


def _broadcast(tmp_path, *, chats=0, messages=0, segments=12, **config):
    """(기록된 작업 목록, aggregate_log 행, 대기 집계)."""
    engine = _engine(tmp_path, corners=["topic_talk"], **config)
    original = engine.scheduler.pipeline.run
    jobs = []

    async def spy(job):
        jobs.append(job)
        return await original(job)

    engine.scheduler.pipeline.run = spy
    # 인용 간격이 막혀 있어야 인용 없는 멘트가 나온다 — 첫 인용에 쓰이는 입력과 분리한다
    engine.scheduler._last_quote_at = time.time()

    async def scenario():
        for i in range(chats):
            await engine.submit_request("story", f"쇼핑 때문에 큰일이에요 {i}", f"u{i}")
        for i in range(messages):
            await engine.submit_request("message", f"문자 내용 번호{i}입니다", f"m{i}")
        await engine.run(max_segments=segments)

    asyncio.run(scenario())
    db = sqlite3.connect(tmp_path / "m.sqlite")
    try:
        rows = db.execute("SELECT kind, corner_type, keyword, count, total, segment_id "
                          "FROM aggregate_log").fetchall()
    finally:
        db.close()
    return jobs, rows


def test_flood_is_aggregated_once_per_corner_and_logged(tmp_path):
    jobs, rows = _broadcast(tmp_path, chats=8)
    marked = [job for job in jobs if job.material.extra.get("aggregate")]
    assert marked
    assert len({id(job) for job in marked}) == len(rows)
    assert all(not job.material.quotes and not job.material.feature_ref for job in marked)
    assert rows[0][:5] == ("aggregate", marked[0].corner_type, "쇼핑", 8, 8)
    assert rows[0][5]  # 세그먼트와 이어진다
    # 코너 1회당 한 번 — 토크 코너는 단계가 여럿이고 연달아 오지 않으므로 묶음이 곧 코너 1회다
    talks = [list(group) for corner, group in itertools.groupby(jobs, key=lambda job: job.corner_type)
             if corner == "topic_talk"]
    assert talks and max(len(talk) for talk in talks) > 1
    for talk in talks:
        assert sum(1 for job in talk if job.material.extra.get("aggregate")) <= 1


def test_aggregate_is_not_counted_as_personal_reflection(tmp_path):
    _broadcast(tmp_path, chats=8)
    db = sqlite3.connect(tmp_path / "m.sqlite")
    try:
        assert db.execute("SELECT COUNT(*) FROM reflection_log").fetchone()[0] == 0
    finally:
        db.close()


def test_quiet_room_has_no_notice(tmp_path):
    jobs, rows = _broadcast(tmp_path, chats=3)
    assert rows == []
    assert not any(job.material.extra.get("aggregate") for job in jobs)


def test_preview_when_messages_pile_up_and_text_corner_is_far(tmp_path):
    engine = _engine(tmp_path, corners=["topic_talk"])
    scheduler = engine.scheduler
    scheduler._started_at = time.time()
    scheduler._end_at = scheduler._started_at + 1800

    async def fill(n):
        for i in range(n):
            await engine.submit_request("message", f"문자 내용 번호{i}입니다", f"m{i}")

    asyncio.run(fill(4))
    scheduler.clock.next(1e9)  # 오프닝
    # 순환: $main, music_intro, text_corner, music_intro, $main, music_intro
    for _ in range(3):
        scheduler.clock.next(1e9)  # 문자 코너까지 지나간다 — 다음 둘은 music_intro, $main
    now = time.time()
    assert not scheduler._should_preview(now)  # 4건은 부족하다
    asyncio.run(engine.submit_request("message", "문자 내용 번호9입니다", "m9"))
    assert scheduler._should_preview(now)
    scheduler._previewed = True
    assert not scheduler._should_preview(now)  # 문자 코너까지 한 번만
    scheduler._previewed = False
    scheduler._end_at = now + 100
    assert not scheduler._should_preview(now)  # 곧 끝나는 방송에서는 약속하지 않는다
    engine.telemetry.close()


def test_no_preview_when_text_corner_is_near(tmp_path):
    engine = _engine(tmp_path, corners=["topic_talk"])
    scheduler = engine.scheduler
    scheduler._started_at = time.time()
    scheduler._end_at = scheduler._started_at + 1800

    async def fill():
        for i in range(5):
            await engine.submit_request("message", f"문자 내용 번호{i}입니다", f"m{i}")

    asyncio.run(fill())
    scheduler.clock.next(1e9)  # 오프닝 — 다음 둘은 $main, music_intro
    scheduler.clock.next(1e9)  # $main — 다음 둘은 music_intro, text_corner
    assert not scheduler._should_preview(time.time())
    engine.telemetry.close()


def test_experiment_can_turn_notices_off(tmp_path):
    jobs, rows = _broadcast(tmp_path, chats=8, experiment={"aggregate_ack": False})
    assert rows == []
    assert not any(job.material.extra.get("aggregate") for job in jobs)
