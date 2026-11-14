"""방송 중 형식 전환 (#260) — station.format_changed로 클락·코너·입력 계약을 바꾸고, 걸리면 이전 형식으로 계속한다."""
import asyncio
import time
from pathlib import Path

from onair_engine.domain import RequestState
from onair_engine.engine import EngineSettings
from onair_engine.format import Clock, load_format
from onair_engine.manager import EngineManager
from test_corner_skip import _summaries, _until
from test_persona_switch import PERSONA, _switchable_engine
from test_scheduler import _engine, _glue

FORMATS = Path(__file__).resolve().parent.parent / "config" / "formats"
RSS = ("<rss><channel>" + "".join(
    f"<item><title>기사 {i}</title><description>요약 {i}</description><link>https://news.example.com/{i}</link></item>"
    for i in range(4)) + "</channel></rss>").encode()


def _fetch(delay: float = 0.0):
    def fetch(url: str) -> bytes:
        time.sleep(delay)
        return RSS
    return fetch


def test_clock_can_start_without_the_opening_and_format_names_its_entry_corner():
    clock = Clock(load_format("news", FORMATS), sections=["it"], skip_opening=True)
    assert clock.phase == "cycle"
    assert clock.next(600).corner.corner_type == "headline", "오프닝 없이 순환의 첫 코너부터다"
    assert load_format("music", FORMATS).entry_corner == "artist_talk"
    assert load_format("talk", FORMATS).entry_corner is None


def test_switch_to_news_drops_talk_work_and_starts_the_news_cycle(tmp_path):
    engine, transport, pipeline = _switchable_engine(
        tmp_path, lambda job: job.corner_type == "topic_talk", corners=["topic_talk"])
    engine.settings.rss_fetch = _fetch()
    scheduler = engine.scheduler

    async def scenario():
        run = asyncio.create_task(engine.run())
        await _until(lambda: pipeline.waiting == 1, "주제 토크 첫 멘트 생성")
        dropped = pipeline.jobs[-1]
        assert engine.on_format_changed({"format": "news", "news_sections": ["it", "economy"]})
        await _until(lambda: any(job.corner_type == "headline" for job in pipeline.jobs), "헤드라인 생성")
        await engine.submit_request("chat", "뉴스 잘 듣고 있어요", "phone", request_id="chat")
        engine.stop()
        pipeline.open.set()
        await run
        return dropped

    dropped = asyncio.run(scenario())
    config = engine.config
    assert (config.format, config.corners, config.news_sections) == ("news", None, ["it", "economy"])
    assert scheduler.fmt.format == "news" and scheduler.format_switches == 1
    after = [job.corner_type for job in pipeline.jobs[pipeline.jobs.index(dropped) + 1:]]
    assert after[0] == "headline" and "opening" not in after and "topic_talk" not in after
    assert not any(s.id == f"{dropped.step}:{pipeline.jobs.index(dropped) + 1}" for s in transport.segments)
    states = [str(state) for rid, state in transport.states if rid == "chat"]
    assert states[-1] == str(RequestState.EXPIRED), "뉴스는 채팅을 방송에 반영하지 않는다 (v1은 expired로 알린다)"
    summaries = _summaries(tmp_path)
    at = summaries.index("형식 전환 · 사연토크 → 뉴스 · 만들던 멘트 1개 버림")
    assert "코너 종료 · 오늘의 이야기" in summaries[at - 2:at]
    assert any(s.startswith("기사 수집 · ") for s in summaries)
    assert "코너 진입 · 헤드라인" in summaries[at:]
    assert engine.topic == "테스트 방송"


def test_scheduler_waits_for_the_first_news_fetch_before_planning(tmp_path):
    engine, _, pipeline = _switchable_engine(
        tmp_path, lambda job: job.corner_type == "topic_talk", corners=["topic_talk"])
    engine.settings.rss_fetch = _fetch(delay=1.2)  # 편성 루프가 형식을 바꾼 뒤에도 수집이 끝나지 않았다

    async def scenario():
        run = asyncio.create_task(engine.run())
        await _until(lambda: pipeline.waiting == 1, "주제 토크 첫 멘트 생성")
        engine.on_format_changed({"format": "news"})
        await _until(lambda: any(job.corner_type == "headline" for job in pipeline.jobs), "헤드라인 생성")
        engine.stop()
        pipeline.open.set()
        await run

    asyncio.run(scenario())
    summaries = _summaries(tmp_path)
    switched = summaries.index("형식 전환 · 사연토크 → 뉴스 · 만들던 멘트 1개 버림")
    after = summaries[switched:]
    assert "코너 건너뜀 · 헤드라인 (소재 없음)" not in after, "기사가 오기 전에 헤드라인을 건너뛰지 않는다"
    fetched = next(i for i, s in enumerate(after) if s.startswith("기사 수집 · "))
    assert after.index("코너 진입 · 헤드라인") > fetched, "첫 수집을 기다렸다가 헤드라인에 들어간다"


def test_switch_to_music_enters_artist_talk_first_and_back_to_talk_keeps_inputs(tmp_path):
    engine, transport, pipeline = _switchable_engine(
        tmp_path, lambda job: job.corner_type in ("topic_talk", "artist_talk", "text_corner"),
        corners=["topic_talk", "text_corner"])
    scheduler = engine.scheduler

    async def scenario():
        run = asyncio.create_task(engine.run())
        await _until(lambda: pipeline.waiting == 1, "주제 토크 첫 멘트 생성")
        await engine.submit_request("message", "오늘 붕어빵을 먹었어요", "u1", request_id="msg", nickname="밤샘러")
        engine.on_board_story({"story_id": "story_1", "body": "퇴근길에 호떡을 샀어요", "nickname": "눈사람"})
        assert engine.on_format_changed({"format": "music"})
        await _until(lambda: pipeline.waiting == 2, "음악 형식 첫 멘트 생성")
        first_music = pipeline.jobs[-1]
        await engine.submit_request("message", "문자는 음악 방송에 안 나가요", "u2", request_id="late")
        assert scheduler.sources.inbox.count() == 1 and scheduler.sources.stories.count() >= 0
        assert engine.on_format_changed({"format": "talk", "corners": ["text_corner", "topic_talk"]})
        await _until(lambda: pipeline.waiting == 3, "토크로 돌아온 첫 멘트 생성")
        first_talk = pipeline.jobs[-1]
        engine.stop()
        pipeline.open.set()
        await run
        return first_music, first_talk

    first_music, first_talk = asyncio.run(scenario())
    assert (first_music.corner_type, first_music.step) == ("artist_talk", "artist_talk")
    assert first_talk.corner_type == "text_corner" and first_talk.material.feature_ref == "msg", \
        "토크에서 받은 문자는 음악을 거쳐 돌아와도 읽힌다"
    states = {rid: str(state) for rid, state in transport.states}
    assert states["late"] == str(RequestState.EXPIRED), "음악 형식은 문자를 받지 않는다"
    assert "story_1" in {s.request_id for s in scheduler.sources.stories.items}, "사연 풀은 형식을 바꿔도 비우지 않는다"
    summaries = _summaries(tmp_path)
    assert [s.split(" · 만들던")[0] for s in summaries if s.startswith("형식 전환 · ")] == [
        "형식 전환 · 사연토크 → 음악", "형식 전환 · 음악 → 사연토크"]
    assert scheduler.format_switches == 2 and engine.config.corners == ["text_corner", "topic_talk"]


def test_rejected_or_repeated_format_keeps_the_broadcast_as_it_is(tmp_path):
    engine, _, _ = _switchable_engine(tmp_path, lambda job: False, corners=["topic_talk"])

    async def scenario():
        results = [
            engine.on_format_changed({"format": "podcast"}),
            engine.on_format_changed({}),
            engine.on_format_changed({"format": "talk", "corners": ["headline"]}),
            engine.on_format_changed({"format": "news", "news_sections": ["weather"]}),
            engine.on_format_changed({"format": "talk", "corners": ["topic_talk"]}),  # 지금과 같다
        ]
        assert results == [False] * 5
        assert engine._glue_task is None and engine._poller is None
        assert engine.on_format_changed({"format": "music"})
        pending = engine.scheduler._format
        assert not engine.on_format_changed({"format": "music"}), "바꾸는 중인 형식이 다시 왔다"
        assert engine.scheduler._format is pending
        await engine._glue_task

    asyncio.run(scenario())
    engine.telemetry.close()
    rejected = [s for s in _summaries(tmp_path) if s.startswith("형식 전환 거절 · ")]
    assert len(rejected) == 4, "같은 형식이 다시 온 것은 거절이 아니라 재전달이다"
    assert "format" in rejected[0] and "headline" in rejected[2] and "weather" in rejected[3]
    assert engine.config.format == "talk" and engine.scheduler.fmt.format == "talk"


def test_glue_follows_both_switches_whichever_comes_first(tmp_path):
    for first in ("format", "persona"):
        engine, _ = _engine(tmp_path / first, corners=["topic_talk"])

        async def scenario(engine=engine, first=first):
            if first == "format":
                assert engine.on_format_changed({"format": "music"})
                assert await engine.on_persona_changed({"persona": PERSONA})
            else:
                assert await engine.on_persona_changed({"persona": PERSONA})
                assert engine.on_format_changed({"format": "music"})
            await engine._glue_task
            await engine.scheduler._apply_persona()
            await engine.scheduler._apply_format()

        asyncio.run(scenario())
        engine.telemetry.close()
        scheduler = engine.scheduler
        assert scheduler.glue is engine.glue, "마지막에 받은 연결 멘트를 쓴다"
        assert scheduler.glue._fmt is scheduler.fmt, "새 형식의 문구다"
        assert scheduler.glue._profile.dj_name == "번개" and scheduler.pipeline.profile.dj_name == "번개"
        assert scheduler.fmt.format == "music"


def test_corner_changed_carries_the_format_only_when_enabled(tmp_path):
    for enabled in (False, True):
        engine, transport = _engine(tmp_path / str(enabled), corners=["topic_talk"], broadcast_minutes=1)
        engine.scheduler._emit_corner_changed = True
        engine.scheduler._emit_corner_format = enabled
        asyncio.run(engine.run(max_segments=3))
        changed = [payload for event, payload in transport.station_events if event == "corner.changed"]
        assert changed and all(("format" in payload) == enabled for payload in changed)
        if enabled:
            assert {payload["format"] for payload in changed} == {"talk"}
        assert not any(_glue(s) and s.corner_type == "headline" for s in transport.segments)


def test_manager_routes_format_changed(tmp_path):
    engine, _, _ = _switchable_engine(tmp_path, lambda job: False, corners=["topic_talk"])
    manager = EngineManager(EngineSettings())

    async def scenario():
        await manager._route_station_event(engine, {"type": "station.format_changed", "payload": {"format": "music"}})
        assert engine.scheduler._format.fmt.format == "music"
        await engine._glue_task

    asyncio.run(scenario())
    engine.telemetry.close()
