"""코너 변경 (#259) — corner.skip으로 지금 코너를 끝내고 고른 코너로, segment.discarded로 버려진 멘트의 곡·입력 되돌리기."""
import asyncio
import json
import sqlite3
from pathlib import Path

from onair_engine.domain import SegmentKind, SegmentSubmission
from onair_engine.engine import EngineSettings
from onair_engine.format import Clock, load_format
from onair_engine.inputs.on_air import OnAirTimeline
from onair_engine.manager import EngineManager
from test_scheduler import _engine, _glue

FORMATS = Path(__file__).resolve().parent.parent / "config" / "formats"


class Gated:
    """block(job)이 참인 작업은 문을 열 때까지 생성 중으로 머문다. 세그먼트 id는 '단계:순번'이다."""

    tts_blocked_until = 0.0

    def __init__(self, block):
        self.block = block
        self.open = asyncio.Event()
        self.jobs = []
        self.waiting = 0

    async def run(self, job):
        self.jobs.append(job)
        index = len(self.jobs)
        if not self.open.is_set() and self.block(job):
            self.waiting += 1
            await self.open.wait()
        ref = job.material.quotes[0].request_id if job.material.quotes else job.material.feature_ref
        return SegmentSubmission(
            id=f"{job.step}:{index}", station_id="st_test", audio_ref=f"st_test/{job.step}.wav",
            duration_ms=1000, corner_type=job.corner_type, kind=SegmentKind.SPEECH,
            priority=50, reorderable=True, request_ref=ref, music_ref=job.material.track_ref,
        )

    def take_aired(self, segment_id):
        return None


async def _until(condition, what: str) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"기다리던 일이 일어나지 않았다: {what}")


def _summaries(tmp_path) -> list[str]:
    db = sqlite3.connect(tmp_path / "metrics.sqlite")
    try:
        return [row[0] for row in db.execute("SELECT summary FROM event_log ORDER BY rowid")]
    finally:
        db.close()


def _gated_engine(tmp_path, block, **config):
    engine, transport = _engine(tmp_path, **config)
    scheduler = engine.scheduler
    scheduler.pipeline = pipeline = Gated(block)
    scheduler.max_concurrent = 1  # 한 번에 하나씩 — 막힌 작업 뒤로 편성이 계속 나아가지 않게
    return engine, transport, pipeline


def test_clock_jumps_only_to_cycle_corners_while_the_cycle_runs():
    fmt = load_format("talk", FORMATS)
    clock = Clock(fmt, main_corners=["topic_talk"])
    assert clock.jump("text_corner") is None, "오프닝 전에는 바꾸지 않는다"
    clock.next(600)  # 오프닝
    assert clock.jump("text_corner").corner.corner_type == "text_corner"
    assert clock.jump("opening") is None and clock.jump("closing") is None
    assert clock.jump("headline") is None, "이 형식에 없는 코너"
    upcoming = [e.corner.corner_type for e in clock.upcoming(2)]
    clock.jump("text_corner")
    assert [e.corner.corner_type for e in clock.upcoming(2)] == upcoming, "순환 차례는 그대로다"
    clock.next(0)  # 엔딩
    assert clock.jump("text_corner") is None, "엔딩이 시작되면 바꾸지 않는다"

    news = Clock(load_format("news", FORMATS), sections=["it", "economy"])
    news.next(600)
    assert news.jump("section").label == "IT", "분야마다 도는 코너는 첫 분야로 들어간다"
    assert news.jump("headline").label is None


def test_timeline_drops_a_discarded_segment_and_pulls_later_ones_forward():
    timeline = OnAirTimeline()
    anchors = [timeline.new_anchor(f"topic:{i}", "topic", []) for i in range(3)]
    for i, anchor in enumerate(anchors):
        timeline.append(f"s{i}", anchor, 10_000, now=100.0)
    assert timeline.current(125.0) is anchors[2]
    timeline.remove("s1")
    assert timeline.current(109.0) is anchors[0] and timeline.current(111.0) is anchors[2]
    timeline.sync_started("s2", 110.0)
    timeline.remove("s2")  # 재생을 시작한 것은 버려진 것이 아니다
    assert timeline.current(111.0) is anchors[2]
    timeline.remove("unknown")


def test_skip_cuts_the_corner_and_enters_the_chosen_one(tmp_path):
    engine, transport, pipeline = _gated_engine(
        tmp_path, lambda job: job.corner_type not in ("opening", "text_corner"), corners=["topic_talk"])
    scheduler = engine.scheduler

    async def scenario():
        await engine.submit_request("message", "오늘 붕어빵을 먹었어요", "u1", request_id="msg", nickname="밤샘러")
        run = asyncio.create_task(engine.run())
        await _until(lambda: pipeline.waiting == 1, "주제 토크 첫 멘트 생성")
        assert scheduler.current_corner == "topic_talk"
        engine.on_corner_skip({"corner": "text_corner"})
        await _until(lambda: any(s.request_ref == "msg" for s in transport.segments), "문자 낭독 제출")
        await _until(lambda: pipeline.waiting == 2, "문자 소개 다음 코너의 생성")
        assert scheduler.inflight == 1, "버린 생성은 동시 생성 수에 넣지 않는다"
        engine.stop()
        pipeline.open.set()
        await run

    asyncio.run(scenario())
    speech = [s.corner_type for s in transport.segments if not _glue(s)]
    assert speech[:3] == ["opening"] * 3
    assert speech[3] == "text_corner", "생성 중이던 주제 토크 멘트는 나가지 않는다"
    assert not any(s.id == "talk_open:4" for s in transport.segments)
    states = [str(state) for rid, state in transport.states if rid == "msg"]
    assert states[-2:] == ["generating", "generated"]
    summaries = _summaries(tmp_path)
    at = summaries.index("코너 변경 요청 · 문자 소개")
    assert summaries[at + 1] == "코너 종료 · 오늘의 이야기"
    assert "코너 진입 · 문자 소개" in summaries[at:]


def test_inputs_held_by_an_abandoned_generation_go_back_and_are_used_again(tmp_path):
    engine, transport, pipeline = _gated_engine(
        tmp_path, lambda job: bool(job.material.quotes), corners=["topic_talk"])

    async def scenario():
        await engine.submit_request("chat", "요즘 겨울 간식은 뭐가 맛있어요", "phone", request_id="chat")
        run = asyncio.create_task(engine.run())
        await _until(lambda: pipeline.waiting == 1, "채팅을 인용하는 멘트 생성")
        first = pipeline.jobs[-1]
        engine.on_corner_skip({"corner": "topic_talk"})
        await _until(lambda: pipeline.waiting == 2, "되돌아온 채팅을 다시 인용")
        assert pipeline.jobs[-1] is not first and pipeline.jobs[-1].corner_type == "topic_talk"
        pipeline.open.set()
        await _until(lambda: any(s.request_ref == "chat" for s in transport.segments), "인용 멘트 제출")
        engine.stop()
        await run

    asyncio.run(scenario())
    quoting = [s for s in transport.segments if s.request_ref == "chat"]
    assert len(quoting) == 1, "버린 멘트는 나가지 않고 채팅은 한 번만 인용된다"
    states = [str(state) for rid, state in transport.states if rid == "chat"]
    assert states[-4:] == ["generating", "queued", "generating", "generated"]
    assert engine.scheduler._failures.get("chat", 0) == 0, "코너 변경은 입력의 실패로 세지 않는다"


def test_skip_to_a_corner_without_material_falls_back_to_the_cycle(tmp_path):
    engine, _, pipeline = _gated_engine(
        tmp_path, lambda job: job.corner_type != "opening", corners=["topic_talk"])

    async def scenario():
        run = asyncio.create_task(engine.run())
        await _until(lambda: pipeline.waiting == 1, "주제 토크 첫 멘트 생성")
        engine.on_corner_skip({"corner": "text_corner"})  # 문자가 없다
        await _until(lambda: pipeline.waiting == 2, "다음 코너의 생성")
        engine.on_corner_skip({"corner": "headline"})  # 사연토크에 없는 코너
        engine.on_corner_skip({"corner": "closing"})  # 마지막 요청만 본다
        await _until(lambda: any("코너 변경 무시" in s for s in _summaries(tmp_path)), "무시 기록")
        assert pipeline.waiting == 2, "받아들이지 않은 요청은 지금 코너를 끊지 않는다"
        engine.stop()
        pipeline.open.set()
        await run

    asyncio.run(scenario())
    summaries = _summaries(tmp_path)
    at = summaries.index("코너 변경 요청 · 문자 소개")
    assert "코너 건너뜀 · 문자 소개 (소재 없음)" in summaries[at:]
    assert "코너 변경 무시 · 엔딩 (지금은 바꿀 수 없는 코너)" in summaries
    assert not any("headline" in s for s in summaries), "뒤 요청이 앞 요청을 덮었다"


def test_discarded_segments_return_track_and_input_and_leave_the_buffer(tmp_path):
    read = []
    engine, transport, pipeline = _gated_engine(
        tmp_path, lambda job: bool(read) and job.step != "text_read", dummy_tracks=False,
        corners=["topic_talk"])
    scheduler = engine.scheduler
    scheduler._emit_corner_changed = True
    original = pipeline.run

    async def run_and_note(job):
        if job.step == "text_read":
            read.append(job)
        return await original(job)

    pipeline.run = run_and_note

    async def scenario():
        engine.on_track_queued({"track": {"track_ref": "yt:a", "title": "첫눈", "artist": "가수",
                                          "duration_ms": 200_000}, "source": "playlist"})
        await engine.submit_request("message", "오늘 붕어빵을 먹었어요", "u1", request_id="msg")
        run = asyncio.create_task(engine.run())
        await _until(lambda: pipeline.waiting == 1, "문자 낭독 다음 멘트 생성")
        intro = next(s for s in transport.segments if s.music_ref == "yt:a")
        reading = next(s for s in transport.segments if s.request_ref == "msg")
        announced = [p["segment_id"] for kind, p in transport.station_events if kind == "corner.changed"]
        assert engine.tracks.peek() is None
        # 마지막 코너 알림이 실린 세그먼트 — 백엔드는 이것이 재생될 때 화면을 바꾼다
        call = next(s for s in transport.segments if s.id == announced[-1])
        assert call.id not in (intro.id, reading.id)
        produced = scheduler._produced_ms

        await EngineManager(EngineSettings())._route_station_event(engine, {
            "type": "segment.discarded",
            "payload": {"segment_ids": [intro.id, call.id, reading.id, "seg_unknown"]}})

        assert produced - scheduler._produced_ms == 1000 + 200_000 + call.duration_ms + 1000
        assert engine.tracks.peek()["track_ref"] == "yt:a", "소개가 버려진 곡은 다음 곡 소개 차례로"
        assert engine.sources.inbox.count() == 1
        assert scheduler._announced_corner is None, "코너 알림이 실린 세그먼트가 버려졌다 — 다시 알린다"
        await scheduler.discard_segments([reading.id])  # 재전달은 무시한다
        assert engine.sources.inbox.count() == 1
        engine.stop()
        pipeline.open.set()
        await run

    asyncio.run(scenario())
    states = [str(state) for rid, state in transport.states if rid == "msg"]
    assert states[-3:] == ["generated", "queued", "expired"], "받은편지함으로 돌아갔다가 방송 종료로 만료"
    assert "미송출 멘트 3개 버림 · 입력 1건 되돌림" in _summaries(tmp_path)


def test_started_segments_are_not_discarded(tmp_path):
    engine, transport, pipeline = _gated_engine(
        tmp_path, lambda job: job.corner_type != "opening", corners=["topic_talk"])
    scheduler = engine.scheduler

    async def scenario():
        run = asyncio.create_task(engine.run())
        await _until(lambda: pipeline.waiting == 1, "주제 토크 첫 멘트 생성")
        first = transport.segments[0]
        engine.on_segment_started(first.id, 1.0)
        produced = scheduler._produced_ms
        await engine.on_segments_discarded({"segment_ids": [first.id]})
        assert scheduler._produced_ms == produced
        engine.stop()
        pipeline.open.set()
        await run

    asyncio.run(scenario())
    assert not any("버림" in s for s in _summaries(tmp_path))


def test_manager_routes_corner_skip_and_ignores_bad_payloads(tmp_path):
    engine, _, _ = _gated_engine(tmp_path, lambda job: False)
    manager = EngineManager(EngineSettings())

    async def scenario():
        await manager._route_station_event(engine, {"type": "corner.skip", "payload": {"corner": "text_corner"}})
        for bad in ({"type": "corner.skip", "payload": {}},
                    {"type": "segment.discarded", "payload": {"segment_ids": "seg_1"}}):
            try:
                await manager._route_station_event(engine, bad)
            except ValueError:
                continue
            raise AssertionError(f"형식이 틀린 이벤트를 받아들였다: {json.dumps(bad)}")

    asyncio.run(scenario())
    engine.telemetry.close()
    assert engine.scheduler._jump == "text_corner"
