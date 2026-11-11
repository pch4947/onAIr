"""말할 거리가 있을 때만 이야기한다 — 채팅 → 다시 꺼내기 → 갈래의 말할 거리, 엔딩 돌아보기 (#226)."""
import asyncio
import time

from onair_engine.corners.run import CornerRun, PlannedStep, Thread
from onair_engine.corners.steps import CALLBACK_RULE, build_prompt
from onair_engine.domain import ListenerRequest, Material
from onair_engine.format.clock import ClockEntry
from onair_engine.inputs.callbacks import CallbackStore
from onair_engine.inputs.topic_plan import Angle, TopicPlan
from onair_engine.pipeline.llm import detect_step
from onair_engine.prompt_data import CALLBACK, COVERED_ANGLES
from onair_engine.report import render, station_report
from test_scheduler import PROFILE, FakePipeline, _engine


def _req(kind: str, body: str, nickname: str | None = "밤샘러", rid: str = "r1") -> ListenerRequest:
    return ListenerRequest(request_id=rid, kind=kind, body=body, requester_ref="u", received_at=0.0,
                           nickname=nickname)


# ── 다시 꺼낼 이야기 ───────────────────────────────────────────────────

def test_callbacks_wait_then_come_back_once_each():
    store = CallbackStore(min_age_sec=180)
    store.add(_req("message", "퇴근길에 붕어빵 샀어요"), aired_at=1000)
    store.add(_req("song_request", " "), aired_at=1000)  # 한마디 없는 신청곡 — 다시 꺼낼 말이 없다
    store.add(_req("chat", "호떡파입니다"), aired_at=1100)
    assert len(store) == 2
    assert store.take(1100) is None, "방금 한 얘기를 곧바로 되풀이하지 않는다"
    first = store.take(1200)
    assert first.body == "퇴근길에 붕어빵 샀어요" and store.take(1200) is None
    assert store.take(1300).body == "호떡파입니다" and store.take(9999) is None


def test_callback_text_names_only_readers_not_chatters():
    store = CallbackStore(min_age_sec=0)
    store.add(_req("message", "문자 본문"), aired_at=0)
    store.add(_req("chat", "채팅 본문"), aired_at=0)
    message, chat = store.take(1), store.take(1)
    assert message.text() == "종류: 문자\n닉네임: 밤샘러\n문자 본문"
    assert chat.text() == "종류: 채팅\n채팅 본문", "채팅은 닉네임을 말하지 않는다"


def test_lookback_spreads_over_the_broadcast():
    store = CallbackStore()
    for i in range(7):
        store.add(_req("chat", f"채팅 {i}"), aired_at=i)
    assert [a.body for a in store.lookback(3)] == ["채팅 0", "채팅 3", "채팅 6"]
    assert len(store.lookback(3)) == 3 and store.take(10**6) is not None, "돌아보기는 다시 꺼내기를 쓰지 않는다"


# ── 말할 거리 순서 ─────────────────────────────────────────────────────

def _scheduler_run(tmp_path, angle: Angle | None):
    engine, _ = _engine(tmp_path, corners=["topic_talk"], topic="겨울 간식")
    scheduler = engine.scheduler
    scheduler.topic_plan = plan = TopicPlan(None, "겨울 간식", count=1, violates=lambda text: None)
    if angle is not None:  # 꺼내는 단계가 첫 말할 거리를 이미 썼다
        plan._angles = [angle]
        plan.take(1)
    run = CornerRun(ClockEntry(scheduler.fmt.corner("topic_talk")), sources=scheduler.sources,
                    intensity=scheduler.intensity, thread=Thread(topic="겨울 간식", angle=angle))
    scheduler._run = run
    return scheduler, run


def test_talk_points_prefer_chat_then_callback_then_angle(tmp_path):
    scheduler, run = _scheduler_run(tmp_path, Angle("호빵", ("데우는 법", "팥과 야채", "편의점 풍경", "남은 한 개")))
    scheduler.callbacks.min_age_sec = 0
    scheduler.callbacks.add(_req("message", "퇴근길 붕어빵"), aired_at=0)
    scheduler.callbacks.add(_req("message", "호떡 좋아요", rid="r2"), aired_at=0)
    scheduler.enqueue_request(_req("chat", "저는 호빵파예요", rid="c1"))
    assert scheduler._talk_point(run) == {"source": "chat"}
    scheduler._last_quote_at = time.time()  # 인용 간격 안 — 채팅을 쓸 수 없다
    point = scheduler._talk_point(run)
    assert point["source"] == "callback" and point["callback"].body == "퇴근길 붕어빵"
    assert [scheduler._talk_point(run) for _ in range(3)] == [
        {"source": "angle", "point": "팥과 야채"}, {"source": "angle", "point": "편의점 풍경"}, None,
    ], "다시 꺼내기는 코너당 1번, 꺼내기가 쓴 첫 말할 거리는 빼고, 한 번 들어와서 둘까지만 더 쓴다"
    assert scheduler.topic_plan.more() == ("남은 한 개",), "남은 말할 거리는 다음에 들어와서 잇는다 (#262)"


def test_without_an_angle_topic_talk_gets_one_free_point(tmp_path):
    scheduler, run = _scheduler_run(tmp_path, None)
    assert [scheduler._talk_point(run), scheduler._talk_point(run)] == [{"source": "topic"}, None]


# ── 프롬프트 ───────────────────────────────────────────────────────────

def test_callback_prompt_carries_the_aired_text():
    store = CallbackStore(min_age_sec=0)
    store.add(_req("message", "퇴근길에 붕어빵 샀어요"), aired_at=0)
    user = build_prompt("talk", Material(topic="겨울 간식", extra={"callback": store.take(1)}), PROFILE).user
    assert detect_step(user) == "talk" and CALLBACK_RULE in user
    assert f'<data label="{CALLBACK}">\n종류: 문자\n닉네임: 밤샘러\n퇴근길에 붕어빵 샀어요\n</data>' in user


def test_closing_lookback_uses_aired_inputs_and_angles():
    store = CallbackStore()
    store.add(_req("chat", "호떡파입니다"), aired_at=0)
    m = Material(extra={"lookback": store.lookback(), "covered_angles": ["첫입의 온도", "길 위의 간식"]})
    user = build_prompt("closing_lookback", m, PROFILE).user
    assert detect_step(user) == "closing_lookback"
    assert user.count(f'<data label="{CALLBACK}">') == 1 and COVERED_ANGLES in user
    plain = build_prompt("closing_lookback", Material(), PROFILE).user
    assert CALLBACK not in plain and COVERED_ANGLES not in plain, "기록이 없으면 지금처럼"


# ── 방송 ──────────────────────────────────────────────────────────────

class Recording(FakePipeline):
    def __init__(self):
        super().__init__()
        self.jobs = []

    async def run(self, job):
        self.jobs.append(job)
        return await super().run(job)


def test_broadcast_calls_back_an_aired_message_and_reports_materials(tmp_path):
    engine, _ = _engine(tmp_path, corners=["topic_talk"], topic="겨울 간식")
    engine.scheduler.pipeline = pipeline = Recording()
    engine.topic_plan._llm.delay_sec = 0
    engine.scheduler.callbacks.min_age_sec = 0
    engine.scheduler.callbacks.add(_req("message", "퇴근길에 붕어빵 샀어요"), aired_at=0)  # 앞서 방송에 나갔다
    asyncio.run(engine.run(max_segments=20))
    talks = [j for j in pipeline.jobs if j.step == "talk"]
    assert [j.material.extra["material"] for j in talks[:2]] == ["callback", "angle"]
    assert sum(j.material.extra.get("material") == "callback" for j in talks) == 1

    report = station_report(tmp_path / "metrics.sqlite", "st_test")
    materials = report["generation"]["talk_material"]
    assert materials["callback"] == 1 and materials["angle"] >= 1 and set(materials) <= {"callback", "angle"}
    assert "이야기 단계 소재: 갈래" in render(report)


def test_closing_lookback_gets_aired_inputs_and_covered_angles(tmp_path):
    engine, _ = _engine(tmp_path, corners=["topic_talk"], topic="겨울 간식")
    scheduler = engine.scheduler
    scheduler.callbacks.add(_req("message", "퇴근길에 붕어빵 샀어요"), aired_at=0)
    asyncio.run(engine.topic_plan.run())
    engine.topic_plan.next_angle()
    closing = scheduler.fmt.corner("closing")
    step = next(s for s in closing.steps if s.kind == "closing_lookback")
    job = asyncio.run(scheduler._build_job(PlannedStep(closing, step, [], None)))
    assert [a.body for a in job.material.extra["lookback"]] == ["퇴근길에 붕어빵 샀어요"]
    assert job.material.extra["covered_angles"] == ["1번째 이야기"]
