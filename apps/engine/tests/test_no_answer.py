"""답을 받는 멘트가 "답으로 온 글은 없어서"라고 말하면 그 말을 걷어낸다 (#348).

곡이 나가는 동안 글을 여덟 건 보낸 청취자에게 답이 없다고 말했다 (2026-11-20 실제 방송).
"""
import asyncio

import pytest

from onair_engine.domain import GenerationJob, Material, Script
from onair_engine.pipeline.pipeline import drop_no_answer
from test_scheduler import _engine

AIRED = ("아까는 다음 끼니에 챙길 반찬 재료를 여쭤봤죠. 답으로 온 글은 없어서, 저는 달걀이나 냉장고 속 채소처럼 "
         "익숙한 재료부터 챙겨 두고 싶어요. 혼자 먹는 밥도 그릇에 담아 식탁에 놓으면, 한 끼를 제대로 대접하는 기분이 들죠.")
FIXED = ("아까는 다음 끼니에 챙길 반찬 재료를 여쭤봤죠. 저는 달걀이나 냉장고 속 채소처럼 "
         "익숙한 재료부터 챙겨 두고 싶어요. 혼자 먹는 밥도 그릇에 담아 식탁에 놓으면, 한 끼를 제대로 대접하는 기분이 들죠.")


def test_the_clause_is_dropped_and_the_answer_stays():
    assert drop_no_answer(AIRED) == FIXED


@pytest.mark.parametrize(("script", "expected"), [
    ("반찬 재료를 여쭤봤죠. 아직 답은 없네요. 저는 두부를 챙기고 싶어요.", "반찬 재료를 여쭤봤죠. 저는 두부를 챙기고 싶어요."),
    ("반찬 재료를 여쭤봤죠. 대답이 없으니 제가 먼저 말할게요, 저는 두부예요.", "반찬 재료를 여쭤봤죠. 제가 먼저 말할게요, 저는 두부예요."),
    ("반찬 재료를 여쭤봤죠. 답장은 안 왔지만 저는 두부를 챙기고 싶어요.", "반찬 재료를 여쭤봤죠. 저는 두부를 챙기고 싶어요."),
    ("답이 없어서요. 저는 두부를 챙기고 싶어요.", "저는 두부를 챙기고 싶어요."),
])
def test_other_ways_of_saying_it(script, expected):
    assert drop_no_answer(script) == expected


@pytest.mark.parametrize("script", [
    "두부라고 답해 주신 분, 든든하겠어요. 저는 애호박이 없으면 허전하더라고요.",
    "마음이 답답할 때는 없으니만 못한 말도 있죠. 그래도 천천히 가요.",
    "정답은 없는 질문이었죠. 저는 두부를 챙기고 싶어요.",
    "해답이 없어 보여도 괜찮아요.",
])
def test_other_talk_is_left_alone(script):
    assert drop_no_answer(script) == script


def test_script_that_would_vanish_is_left_as_it_is():
    assert drop_no_answer("답은 아직 없네요.") == "답은 아직 없네요."


class Says:
    def __init__(self, text):
        self.text = text

    async def generate(self, prompt):
        return Script(text=self.text)


@pytest.mark.parametrize(("asked", "expected"), [("다음 끼니에 챙길 반찬 재료는?", FIXED), (None, AIRED)])
def test_only_the_answer_ment_is_checked(tmp_path, asked, expected):
    engine, _ = _engine(tmp_path)
    pipeline = engine.scheduler.pipeline
    pipeline.llm = Says(AIRED)
    stages = []
    pipeline._log = lambda job, stage, started, finished, result: stages.append(f"{stage}:{result}")
    job = GenerationJob(job_id="j", corner_type="topic_talk", step="talk_resume",
                        material=Material(topic="건강한 식습관", extra={"asked": asked} if asked else {}))
    script, _ = asyncio.run(pipeline._write(job))
    assert script.text == expected
    assert ("no_answer:trimmed" in stages) is bool(asked)
