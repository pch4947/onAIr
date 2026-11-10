"""곡 소개는 늦게 만든다 — 곡 직전에 온 채팅이 곡 뒤로 밀리지 않게 (#239)."""
import asyncio
import time

from onair_engine.corners.run import PlannedStep
from onair_engine.domain import ScheduleContext
from test_scheduler import FakePipeline, _engine


def _ctx(buffer: float, inflight: int = 0) -> ScheduleContext:
    return ScheduleContext(now=time.time(), pending_requests=[], generated_buffer_sec=buffer,
                           inflight_generations=inflight, backpressure=False, corner=None,
                           step_index=0, remaining_sec=600)


def _planned(scheduler, corner: str) -> PlannedStep:
    spec = scheduler.fmt.corner(corner)
    return PlannedStep(spec, spec.steps[0], [], None)


def test_intro_waits_until_the_buffer_runs_down(tmp_path):
    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    scheduler = engine.scheduler
    scheduler.target_buffer_sec = 30
    intro = _planned(scheduler, "music_intro")
    assert scheduler._too_early_for_song_intro(intro, _ctx(25))
    assert not scheduler._too_early_for_song_intro(intro, _ctx(20))
    assert scheduler._too_early_for_song_intro(intro, _ctx(15, inflight=1)), "앞 멘트가 아직 버퍼에 안 잡혔다"
    assert not scheduler._too_early_for_song_intro(intro, _ctx(9, inflight=1)), "늦으면 함께 만든다"
    assert not scheduler._too_early_for_song_intro(_planned(scheduler, "topic_talk"), _ctx(25))


class Recording(FakePipeline):
    def __init__(self):
        super().__init__()
        self.jobs = []

    async def run(self, job):
        self.jobs.append(job)
        return await super().run(job)


def _queue(engine, ref: str, title: str) -> None:
    engine.on_track_queued({"track": {"track_ref": ref, "title": title, "artist": "가수",
                                      "duration_ms": 200_000}, "source": "playlist"})


def test_chat_and_newer_track_that_arrive_while_waiting_reach_the_intro(tmp_path):
    engine, _ = _engine(tmp_path, dummy_tracks=False, corners=["topic_talk"])
    scheduler = engine.scheduler
    scheduler.pipeline = pipeline = Recording()
    scheduler.target_buffer_sec = 3  # 곡 소개는 버퍼 2초 이하일 때 — 멘트 하나가 1초라 금방 온다
    _queue(engine, "yt:old", "먼저 정한 곡")

    async def listener():
        while scheduler._deferred is None:
            await asyncio.sleep(0.01)
        await engine.submit_request("chat", "오늘 너무 피곤해요", "phone", request_id="in_tired")
        _queue(engine, "yt:new", "새로 정한 곡")

    async def scenario():
        task = asyncio.create_task(listener())
        await asyncio.wait_for(engine.run(max_segments=7), timeout=30)
        await task

    asyncio.run(scenario())
    intro = next(j for j in pipeline.jobs if j.step == "music_intro")
    assert [q.request_id for q in intro.material.quotes] == ["in_tired"], "기다리는 동안 온 채팅을 인용한다"
    assert intro.material.track["track_ref"] == "yt:new", "백엔드가 틀 곡을 소개한다"
