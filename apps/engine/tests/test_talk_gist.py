"""갈래마다 '하려는 말'을 붙여 DJ의 이야기에 알맹이를 넣는다 (#374, T-022).

갈래가 장면 조각의 나열이면 대본이 감각 묘사와 위로로 채워진다. 같은 방 12개를 네 가지로 방송해 가려 놓고
고르게 했을 때(두 사람, 22번) 예전 방식은 "가장 좋음" 0번, "가장 별로" 15번이었다.
"""
import asyncio
import json
from pathlib import Path

import pytest

from onair_engine.corners.steps import (
    BLOCK_STEP,
    DJ_OPINION_RULE,
    GIST_BLOCK_RULE,
    GIST_TALK_RULE,
    PLAIN_WORDS_RULE,
    build_prompt,
)
from onair_engine.domain import Material
from onair_engine.inputs.topic_plan import (
    EMPATHY_RULE,
    GIST_RULES,
    OPINION_RULE,
    Angle,
    TopicPlan,
    parse_angles,
)
from onair_engine.inputs.topic_plan import (
    build_prompt as plan_prompt,
)
from onair_engine.main import load_config
from test_scheduler import PROFILE

OLD_LINE = "말할 거리는 떠오르는 장면, 흔히 겪는 일, 둘 중 하나 고르기처럼 구체적으로 쓴다"
PLAN = json.dumps({"angles": [
    {"title": "운동복만 꺼내 놓은 날", "gist": "운동은 큰 결심보다 작게 시작하는 편이 오래 간다.",
     "points": ["운동복을 꺼내 놓고 다시 눕는 일", "헬스장까지 가는 길이 제일 멀다", "양말만 신어 보기", "내일도 할 수 있는 만큼만"]},
    {"title": "함께 하는 운동", "points": ["약속이 있으면 나가게 된다"]},
]}, ensure_ascii=False)


class Says:
    def __init__(self, text):
        self.text, self.prompts = text, []

    async def complete(self, prompt, *, max_tokens):
        self.prompts.append(prompt)
        return self.text


# ── 갈래 계획 ──────────────────────────────────────────────────────────

def test_plan_prompt_asks_for_a_gist_only_when_turned_on():
    off = plan_prompt("헬스장 가기 싫은 날", 2).user
    assert OLD_LINE in off and "gist" not in off, "끄면 예전 프롬프트 그대로다"
    on = plan_prompt("헬스장 가기 싫은 날", 2, gist_level=1).user
    assert all(rule in on for rule in GIST_RULES) and OPINION_RULE in on and OLD_LINE not in on
    assert '"gist": "하려는 말 한 문장"' in on and EMPATHY_RULE not in on
    assert EMPATHY_RULE in plan_prompt("오늘 면접 망했어요", 2, gist_level=2).user


def test_gist_is_read_and_shown_with_the_angle():
    first, second = parse_angles(PLAN)
    assert first.gist == "운동은 큰 결심보다 작게 시작하는 편이 오래 간다."
    assert first.text().split("\n")[:2] == ["운동복만 꺼내 놓은 날", "하려는 말: 운동은 큰 결심보다 작게 시작하는 편이 오래 간다."]
    assert second.gist == "" and "하려는 말" not in second.text(), "gist가 없는 갈래는 예전처럼 다룬다"


def test_gist_stays_with_the_angle_across_visits():
    """갈래 하나는 두 번에 걸쳐 나간다 — 둘째 진입도 같은 '하려는 말'을 향한다."""
    llm = Says(PLAN)
    plan = TopicPlan(llm, "헬스장 가기 싫은 날", count=2, violates=lambda text: None, gist_level=2)
    asyncio.run(plan.run())
    assert "gist" in llm.prompts[0].user
    first, state = plan.take(3)
    again, state2 = plan.take(3)
    assert (state, state2) == ("new", "continue")
    assert first.gist == again.gist != "" and first.level == again.level == 2
    assert first.points != again.points and again.points == ("내일도 할 수 있는 만큼만",)


# ── 대본 프롬프트 ──────────────────────────────────────────────────────

def _block(angle):
    return build_prompt(BLOCK_STEP, Material(topic="헬스장 가기 싫은 날", extra={"parts": 4, "angle": angle}), PROFILE).user


def test_block_is_written_toward_the_gist():
    angle = Angle("운동복만 꺼내 놓은 날", ("운동복을 꺼내 놓고 다시 눕는 일",), gist="작게 시작하는 편이 오래 간다.", level=1)
    user = _block(angle)
    assert GIST_BLOCK_RULE in user and DJ_OPINION_RULE in user and "하려는 말: 작게 시작하는 편이 오래 간다." in user
    assert "이번 갈래의 말할 거리를 문단에 차례로 풀어 쓴다" not in user and PLAIN_WORDS_RULE not in user
    assert PLAIN_WORDS_RULE in _block(Angle(angle.title, angle.points, gist=angle.gist, level=2))


@pytest.mark.parametrize("angle", [
    Angle("운동복만 꺼내 놓은 날", ("운동복을 꺼내 놓고 다시 눕는 일",)),
    Angle("운동복만 꺼내 놓은 날", ("운동복을 꺼내 놓고 다시 눕는 일",), level=2),  # 계획이 gist를 못 냈다
])
def test_angle_without_a_gist_is_written_the_old_way(angle):
    user = _block(angle)
    assert "이번 갈래의 말할 거리를 문단에 차례로 풀어 쓴다" in user and GIST_BLOCK_RULE not in user


def test_single_talk_ment_also_follows_the_gist():
    angle = Angle("운동복만 꺼내 놓은 날", ("양말만 신어 보기",), gist="작게 시작하는 편이 오래 간다.", level=2)
    user = build_prompt("talk", Material(topic="헬스장 가기 싫은 날", extra={"angle": angle}), PROFILE).user
    assert GIST_TALK_RULE in user and PLAIN_WORDS_RULE in user


def test_song_story_can_be_told_in_plain_words():
    extra = {"parts": 4, "song_story": {"title": "새벽 버스", "artist": "모의 밴드"}}
    assert PLAIN_WORDS_RULE not in build_prompt(BLOCK_STEP, Material(topic="운동", extra=dict(extra)), PROFILE).user
    assert PLAIN_WORDS_RULE in build_prompt(BLOCK_STEP, Material(topic="운동", extra={**extra, "plain_words": True}), PROFILE).user


def test_operating_config_turns_it_on():
    _, settings = load_config(Path("config/station.example.yaml"))
    assert settings.talk_gist >= 2  # 3은 2의 내용에 페르소나를 앞세운다 (#376)
