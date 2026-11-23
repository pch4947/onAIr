"""갈래 계획이 DJ를 알고 '풀어 가는 방식'을 함께 낸다 (설정 talk_gist 4, T-025).

갈래를 짜는 단계가 DJ를 모르면 어느 DJ에게나 같은 교훈이 간다. 같은 방을 두 방식으로 방송해도 갈래는 거의 같았고,
하려는 말 24개가 모두 "~가 중요하다 / ~하는 편이 낫다" 꼴이었다. 텐션 높은 DJ의 느낌표는 오프닝 54%에서
첫 이야기 대목 2~5%로 떨어졌다.
"""
import asyncio
import json

from onair_engine.corners.steps import BLOCK_STEP, GIST_FREE_RULE, GIST_HOW_RULE, build_prompt
from onair_engine.domain import Material, StationProfile
from onair_engine.inputs.topic_plan import (
    HOW_EMPATHY_RULE,
    HOW_MENU,
    HOW_RULES,
    ORDER_RULE,
    Angle,
    TopicPlan,
    dj_text,
    parse_angles,
)
from onair_engine.inputs.topic_plan import (
    build_prompt as plan_prompt,
)
from onair_engine.prompt_data import DJ_PROFILE
from test_scheduler import PROFILE, _engine

DJ = StationProfile(dj_name="텐션봇", tone="밝고 경쾌한 호흡의 반말. 맞장구를 빠르게 얹는다", concept="웃음 스위치",
                    energy="high", humor="often", voice_first=True)
PLAN = json.dumps({"angles": [
    {"title": "월요일 편들기 재판", "how": "월요일 탓과 주말 탓을 두 후보로 세워 판정한다",
     "gist": "월요일보다 주말에서 넘어오는 간극이 더 얄밉다는 쪽에 한 표다.",
     "points": ["알람을 끄고 이불 속에서 버티는 몇 초", "주말 저녁의 늦은 야식"]},
    {"title": "작은 시동", "gist": "첫 동작 하나가 재미있다.", "points": ["메일 한 통"]},
]}, ensure_ascii=False)


class Says:
    def __init__(self, text):
        self.text, self.prompts = text, []

    async def complete(self, prompt, *, max_tokens):
        self.prompts.append(prompt)
        return self.text


def test_plan_is_told_who_the_dj_is_only_at_level_four():
    three = plan_prompt("월요병", 2, gist_level=3, dj=dj_text(DJ)).user
    assert ORDER_RULE in three and DJ_PROFILE not in three and '"how"' not in three, "설정 3은 예전 프롬프트 그대로다"
    four = plan_prompt("월요병", 2, gist_level=4, dj=dj_text(DJ), energy="high").user
    assert all(rule in four for rule in HOW_RULES) and HOW_EMPATHY_RULE in four
    assert HOW_MENU["high"] in four and HOW_MENU["low"] not in four, "방식의 목록은 DJ의 에너지로 고른다"
    assert HOW_MENU["low"] in plan_prompt("월요병", 2, gist_level=4, dj=dj_text(DJ), energy="low").user
    assert ORDER_RULE not in four, "처음·가운데·끝으로 맞추라는 지시는 뺀다 — 재료는 방식에 맞춘다"
    assert '"how": "풀어 가는 방식 한 줄"' in four
    assert "컨셉: 웃음 스위치" in four and "에너지: 텐션이 높다" in four and "농담: 농담을 자주 한다" in four


def test_plan_without_a_dj_stays_as_before():
    """DJ를 넘기지 않는 호출(테스트, 예전 호출부)은 설정 4여도 방식을 묻지 않는다."""
    user = plan_prompt("월요병", 2, gist_level=4).user
    assert ORDER_RULE in user and '"how"' not in user


def test_how_is_read_and_shown_with_the_angle():
    first, second = parse_angles(PLAN)
    assert first.how == "월요일 탓과 주말 탓을 두 후보로 세워 판정한다"
    assert first.text().split("\n")[:3] == [
        "월요일 편들기 재판", "풀어 가는 방식: 월요일 탓과 주말 탓을 두 후보로 세워 판정한다",
        "하려는 말: 월요일보다 주말에서 넘어오는 간극이 더 얄밉다는 쪽에 한 표다."]
    assert second.how == "" and "풀어 가는 방식" not in second.text()


def test_how_stays_with_the_angle_across_visits():
    llm = Says(PLAN)
    plan = TopicPlan(llm, "월요병", count=2, violates=lambda text: None, gist_level=4, dj=dj_text(DJ), energy="high")
    asyncio.run(plan.run())
    assert DJ_PROFILE in llm.prompts[0].user and HOW_MENU["high"] in llm.prompts[0].user
    first, _ = plan.take(1)
    again, state = plan.take(1)
    assert state == "continue" and first.how == again.how != ""


def _block(angle: Angle) -> str:
    extra = {"parts": 4, "angle": angle, "voice_first": True}
    return build_prompt(BLOCK_STEP, Material(topic="월요병", extra=extra), PROFILE).user


def test_block_follows_the_format_when_the_angle_has_one():
    angle = parse_angles(PLAN)[0]
    user = _block(Angle(angle.title, angle.points, gist=angle.gist, level=4, how=angle.how))
    assert GIST_HOW_RULE in user and GIST_FREE_RULE not in user
    assert "풀어 가는 방식: 월요일 탓과 주말 탓을 두 후보로 세워 판정한다" in user


def test_block_without_a_format_is_written_as_at_level_three():
    """계획이 방식을 못 냈거나(찾아 둔 이야깃거리를 그대로 쓴 때) 설정 3이면 지금 지시 그대로다."""
    user = _block(Angle("작은 시동", ("메일 한 통",), gist="첫 동작 하나가 재미있다.", level=4))
    assert GIST_FREE_RULE in user and GIST_HOW_RULE not in user


def test_engine_hands_the_dj_to_the_plan(tmp_path):
    engine, _ = _engine(tmp_path, corners=["topic_talk"], topic="월요병")
    engine._talk_gist = 4
    plan = engine._make_topic_plan(engine.scheduler.fmt)
    engine.telemetry.close()
    assert plan is not None and plan.gist_level == 4 and plan._dj == dj_text(engine.config.profile)
    assert plan._energy == engine.config.profile.energy
