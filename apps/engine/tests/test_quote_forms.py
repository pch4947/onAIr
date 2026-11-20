"""채팅을 받는 말의 꼴을 멘트마다 돌려 쓴다 (#352, T-021).

프롬프트의 예시가 "~라고 하신 분" 하나뿐일 때 방송 40개의 채팅 멘트 230건 가운데 82%가 그 꼴이었다.
"""
import time

import pytest

from onair_engine.corners.steps import (
    ANSWER_QUOTE_RULE,
    ASKED_FORM,
    QUOTE_RULE,
    SAID_FORM,
    VARY_FORM_RULE,
    build_prompt,
)
from onair_engine.domain import ListenerRequest, Material
from onair_engine.inputs.rules import load_chat_rules
from onair_engine.pipeline.pipeline import attributed, quoted
from test_scheduler import PROFILE, _engine


def _chat(body: str, rid: str = "c1") -> ListenerRequest:
    return ListenerRequest(request_id=rid, kind="chat", body=body, requester_ref=rid, received_at=time.time())


def test_prompt_shows_the_form_it_was_given():
    material = Material(topic="겨울 간식", quotes=[_chat("저는 붕어빵은 팥이에요")],
                        extra={"quote_forms": ("~라는 글", "~라고 보내 주신 분", "~냐는 질문")})
    user = build_prompt("talk", material, PROFILE).user
    assert "'~라는 글'처럼 청취자의 말임을 밝힌다" in user and "'~냐는 질문'처럼" in user
    assert SAID_FORM not in user and ASKED_FORM not in user
    assert "다음 건은" not in user, "한 건만 받을 때는 다음 꼴을 말하지 않는다"


def test_several_quotes_in_one_ment_get_different_forms():
    material = Material(topic="겨울 간식", quotes=[_chat("저는 팥이에요"), _chat("저는 슈크림이요", "c2")],
                        extra={"quote_forms": ("~라는 글", "~라고 보내 주신 분", "~냐는 질문")})
    user = build_prompt("talk", material, PROFILE).user
    assert VARY_FORM_RULE.format(other="~라고 보내 주신 분") in user
    answers = Material(topic="겨울 간식", quotes=[_chat("저는 팥이에요")],
                       extra={"quote_forms": ("~라는 글", "~라고 보내 주신 분", "~냐는 질문"),
                              "asked": "붕어빵은 팥인가요?", "optional_quotes": True})
    user = build_prompt("talk_resume", answers, PROFILE).user
    assert "한 건씩 '~라는 글'처럼" in user and VARY_FORM_RULE.format(other="~라고 보내 주신 분") in user


def test_without_rotation_the_prompt_is_what_it_was():
    material = Material(topic="겨울 간식", quotes=[_chat("저는 붕어빵은 팥이에요")])
    assert QUOTE_RULE in build_prompt("talk", material, PROFILE).user
    answers = Material(topic="겨울 간식", quotes=[_chat("저는 팥이에요")],
                       extra={"asked": "붕어빵은 팥인가요?", "optional_quotes": True})
    assert ANSWER_QUOTE_RULE in build_prompt("talk_resume", answers, PROFILE).user


@pytest.mark.parametrize("rotate", [True, False])
def test_scheduler_moves_to_the_next_form_each_time_it_takes_a_chat(tmp_path, rotate):
    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    scheduler = engine.scheduler
    forms, asks = ["~라고 하신 분", "~라는 글", "~라고 보내 주신 분"], ["~냐고 물어보신 분", "~냐는 질문"]
    scheduler.chat_rules = scheduler.chat_rules.model_copy(
        update={"quote_rotation": rotate, "quote_forms": forms, "ask_forms": asks})
    seen = []
    for n in range(4):
        material = Material(quotes=[_chat("저는 팥이에요", f"c{n}")])
        scheduler._rotate_quote_form(material)
        seen.append(material.extra.get("quote_forms"))
    empty = Material()
    scheduler._rotate_quote_form(empty)
    engine.telemetry.close()
    assert "quote_forms" not in empty.extra, "채팅을 받지 않는 멘트는 차례를 쓰지 않는다"
    if not rotate:
        assert seen == [None] * 4
        return
    assert [s[0] for s in seen] == [forms[0], forms[1], forms[2], forms[0]]
    assert [s[1] for s in seen] == [forms[1], forms[2], forms[0], forms[1]], "여러 건을 받을 때 쓸 다음 꼴"
    assert [s[2] for s in seen] == [asks[0], asks[1], asks[0], asks[1]]


def test_every_configured_form_counts_as_naming_the_listener():
    """새 꼴을 인용 검사가 알아보지 못하면 받은 글을 되돌려 다음 멘트가 또 받는다."""
    rules = load_chat_rules("config/chat_rules.yaml")
    assert rules.quote_rotation and len(rules.quote_forms) >= 3 and len(rules.ask_forms) >= 2
    for form in rules.quote_forms + rules.ask_forms:
        script = "겨울엔 팥 붕어빵이 제일이" + form.lstrip("~") + ", 저도 그래요."
        assert attributed(script), form
    assert quoted("겨울엔 역시 붕어빵이죠 팥붕 최고", "겨울엔 팥 붕어빵이 제일이라는 글이 왔어요. 저도 팥이에요.")
    assert not attributed("오늘은 붕어빵 이야기를 해 볼게요."), "DJ 자신의 말은 밝히는 말이 아니다"
