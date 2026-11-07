"""이야기 갈래 계획과 이어 말하기 — 주제 토크가 들어올 때마다 주제를 다시 소개하지 않는다 (#224)."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from onair_engine.corners.run import CornerRun, MaterialSources, Thread, TrackSource
from onair_engine.corners.steps import build_prompt
from onair_engine.domain import Material, Prompt
from onair_engine.format import load_format
from onair_engine.format.clock import ClockEntry
from onair_engine.inputs.topic_plan import Angle, TopicPlan, angle_count, parse_angles
from onair_engine.pipeline.llm import DummyLlmClient, LlmError, detect_step
from onair_engine.prompt_data import ANGLE, COVERED_ANGLES, NOW
from onair_engine.scheduler.scheduler import time_context
from onair_engine.sources.rss import RssCollector
from onair_engine.telemetry import Telemetry
from test_scheduler import PROFILE, FakePipeline, _engine, _glue

ANGLES = [Angle("붕어빵 팥 대 슈크림", ("팥파와 슈크림파", "꼬리부터 먹는지")),
          Angle("편의점 호빵", ("데우는 법",))]


# ── 갈래 해석 ──────────────────────────────────────────────────────────

def test_parse_angles_keeps_well_formed_items():
    text = "갈래입니다:\n" + json.dumps({"angles": [
        {"title": " 붕어빵 ", "points": ["팥", " ", 3, "슈크림", "꼬리", "머리"]},
        {"title": ""}, "문자열", {"points": ["제목 없음"]},
        {"title": "호빵", "points": "목록 아님"},
    ]}, ensure_ascii=False)
    angles = parse_angles(text)
    assert angles == [Angle("붕어빵", ("팥", "슈크림", "꼬리")), Angle("호빵", ())]


@pytest.mark.parametrize("bad", ["갈래 없음", '{"angles": "x"}', '{"angles": [{"title": ""}]}', "[1, 2]"])
def test_parse_angles_rejects_unusable_output(bad):
    with pytest.raises(ValueError):
        parse_angles(bad)


def test_angle_count_follows_broadcast_length():
    assert [angle_count(m) for m in (5, 30, 60, 240)] == [2, 6, 12, 12]


# ── 계획 ──────────────────────────────────────────────────────────────

class FixedLlm:
    def __init__(self, reply: str | Exception):
        self.reply = reply
        self.prompts: list[Prompt] = []

    async def complete(self, prompt, *, max_tokens):
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _plan(tmp_path, reply, violates=lambda text: None, **kw):
    telemetry = Telemetry(tmp_path / "m.sqlite", "st_plan")
    llm = FixedLlm(reply)
    return TopicPlan(llm, "겨울 간식", count=3, violates=violates, telemetry=telemetry, **kw), llm, telemetry


def test_plan_hands_out_angles_in_order_and_logs(tmp_path):
    reply = json.dumps({"angles": [{"title": a.title, "points": list(a.points)} for a in ANGLES]},
                       ensure_ascii=False)
    plan, llm, telemetry = _plan(tmp_path, reply, forbidden=["정치 이야기"])
    assert plan.next_angle() is None, "준비 전에는 갈래 없이 간다"
    asyncio.run(plan.run())
    assert "갈래 3개" in llm.prompts[0].user and "정치 이야기" in llm.prompts[0].user
    assert '<data label="지금 화제">\n겨울 간식' in llm.prompts[0].user, "주제는 데이터로 넣는다"
    assert [plan.next_angle(), plan.next_angle(), plan.next_angle()] == [*ANGLES, None]
    assert plan.used == ANGLES
    telemetry.close()
    db = sqlite3.connect(tmp_path / "m.sqlite")
    method, angles = db.execute("SELECT method, angles_json FROM topic_plan_log").fetchone()
    assert method == "llm" and json.loads(angles)[0]["title"] == "붕어빵 팥 대 슈크림"
    events = [s for (s,) in db.execute("SELECT summary FROM event_log WHERE kind = 'topic_plan'")]
    assert events == ["이야기 갈래 2개 준비", "이야기 갈래 1/2 · 붕어빵 팥 대 슈크림",
                      "이야기 갈래 2/2 · 편의점 호빵"]


def test_plan_drops_angles_that_fail_safety_rules(tmp_path):
    reply = json.dumps({"angles": [{"title": "붕어빵", "points": ["example 닷컴에서 주문"]},
                                   {"title": "호빵", "points": []}]}, ensure_ascii=False)
    plan, _, telemetry = _plan(tmp_path, reply, violates=lambda text: "domain" if "닷컴" in text else None)
    asyncio.run(plan.run())
    assert plan.next_angle().title == "호빵" and plan.next_angle() is None
    telemetry.close()
    db = sqlite3.connect(tmp_path / "m.sqlite")
    assert db.execute("SELECT dropped FROM topic_plan_log").fetchone() == (1,)


@pytest.mark.parametrize("reply", [LlmError("429"), "JSON 아님"])
def test_plan_failure_leaves_talk_on_the_topic_alone(tmp_path, reply):
    plan, _, telemetry = _plan(tmp_path, reply)
    asyncio.run(plan.run())
    assert plan.ready and plan.next_angle() is None
    telemetry.close()
    db = sqlite3.connect(tmp_path / "m.sqlite")
    assert db.execute("SELECT method FROM topic_plan_log").fetchone() == ("failed",)


def test_dummy_llm_answers_the_plan():
    plan = TopicPlan(DummyLlmClient(delay_sec=0), "겨울 간식", count=4, violates=lambda t: None)
    asyncio.run(plan.run())
    assert [plan.next_angle().title for _ in range(4)] == [f"{n}번째 이야기" for n in range(1, 5)]


# ── 다시 들어온 코너 ───────────────────────────────────────────────────

def _steps(again: bool) -> list[str]:
    fmt = load_format("talk")
    sources = MaterialSources(tracks=TrackSource(), rss=RssCollector([]))
    run = CornerRun(ClockEntry(fmt.corner("topic_talk")), sources=sources,
                    intensity=fmt.intensity["mid"], thread=Thread(), again=again)
    kinds = []
    while (step := run.next_step()) is not None:
        kinds.append(step.step.glue or step.step.kind)
    return kinds


def test_reentered_topic_talk_resumes_instead_of_reintroducing():
    assert _steps(again=False) == ["call", "talk_open", "talk", "talk", "talk"]
    assert _steps(again=True) == ["resume", "talk_resume", "talk", "talk", "talk"]


def test_glue_again_must_name_a_glue_text():
    from pydantic import ValidationError

    from onair_engine.format.spec import StepSpec
    with pytest.raises(ValidationError):
        StepSpec(kind="glue", glue="call", again="talk_resume")


# ── 프롬프트 ───────────────────────────────────────────────────────────

def test_opener_prompts_carry_angle_covered_list_and_time():
    m = Material(topic="겨울 간식", extra={"angle": ANGLES[1], "covered_angles": [ANGLES[0].title],
                                         "now": "금요일 밤 11시 40분, 방송 18분째", "question": True})
    user = build_prompt("talk_resume", m, PROFILE).user
    assert detect_step(user) == "talk_resume"
    assert "처음 소개하듯 말하지 않" in user
    assert f'<data label="{ANGLE}">\n편의점 호빵\n- 데우는 법\n</data>' in user
    assert f'<data label="{COVERED_ANGLES}">\n- 붕어빵 팥 대 슈크림\n</data>' in user
    assert f'<data label="{NOW}">' in user and "질문" in user

    first = build_prompt("talk_open", Material(topic="겨울 간식", extra={"angle": ANGLES[0]}), PROFILE).user
    assert detect_step(first) == "talk_open" and ANGLE in first and COVERED_ANGLES not in first

    talk = build_prompt("talk", Material(topic="겨울 간식", extra={"angle": ANGLES[0]}), PROFILE).user
    assert detect_step(talk) == "talk" and "아직 하지 않은 것" in talk and NOW not in talk, \
        "시간 맥락은 꺼내는 단계에만 — 매 멘트에 붙이면 되풀이된다"


def test_prompts_without_a_plan_stay_as_before():
    plain = build_prompt("talk", Material(topic="겨울 간식"), PROFILE).user
    assert ANGLE not in plain
    resume = build_prompt("talk_resume", Material(topic="겨울 간식"), PROFILE).user
    assert "다른 면" in resume and ANGLE not in resume


KST = timezone(timedelta(hours=9))


@pytest.mark.parametrize(("at", "elapsed", "expected"), [
    (datetime(2026, 11, 6, 23, 40, tzinfo=KST), 18 * 60 + 30, "금요일 밤 11시 40분, 방송 18분째"),
    (datetime(2026, 11, 7, 0, 5, tzinfo=KST), 30, "토요일 밤 12시 5분, 방송 시작 직후"),
    (datetime(2026, 11, 7, 7, 0, tzinfo=KST), 120, "토요일 아침 7시, 방송 2분째"),
    (datetime(2026, 11, 9, 14, 30, tzinfo=KST), 0, "월요일 오후 2시 30분, 방송 시작 직후"),
])
def test_time_context(at, elapsed, expected):
    now = at.timestamp()
    assert time_context(now, now - elapsed) == expected


# ── 방송 ──────────────────────────────────────────────────────────────

class Recording(FakePipeline):
    def __init__(self):
        super().__init__()
        self.jobs = []

    async def run(self, job):
        self.jobs.append(job)
        return await super().run(job)


def test_broadcast_moves_to_the_next_angle_on_each_topic_talk(tmp_path):
    engine, transport = _engine(tmp_path, corners=["topic_talk"], topic="겨울 간식")
    engine.scheduler.pipeline = pipeline = Recording()
    engine.topic_plan._llm.delay_sec = 0  # 첫 주제 토크 전에 계획이 끝난다 — 실제 방송은 오프닝과 첫 곡이 가려 준다
    asyncio.run(engine.run(max_segments=16))
    openers = [j for j in pipeline.jobs if j.step in ("talk_open", "talk_resume")]
    assert [j.step for j in openers] == ["talk_open", "talk_resume"]
    assert [j.material.extra["angle"].title for j in openers] == ["1번째 이야기", "2번째 이야기"]
    assert "covered_angles" not in openers[0].material.extra
    assert openers[1].material.extra["covered_angles"] == ["1번째 이야기"]
    assert all("now" in j.material.extra for j in openers)
    talks = [j for j in pipeline.jobs if j.step == "talk"]
    assert talks and all(j.material.extra["angle"].title == "1번째 이야기" for j in talks[:3])
    assert [s.audio_ref.rsplit("/", 1)[1].rsplit("_", 1)[0] for s in transport.segments if _glue(s)] \
        == ["topic_talk_call", "topic_talk_resume"]


def test_broadcast_without_host_topic_makes_no_plan(tmp_path):
    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    assert engine.topic_plan is None, "컨셉을 갈래로 펼치지 않는다"
    engine.scheduler.pipeline = pipeline = Recording()
    asyncio.run(engine.run(max_segments=6))
    assert all("angle" not in j.material.extra for j in pipeline.jobs)
