"""DJ 초안이 분위기가 아니라 말하는 방식으로 갈리게 한다 (#377).

예전 프롬프트로 만든 초안 90개에서 예시 멘트의 31%에 위로하는 말이, 농담하는 DJ 예시의 36%에 사물을 사람처럼
말하는 농담이 들었고, 입버릇 "천천히 가자"가 12명에게 겹쳤다.
"""
import asyncio

from onair_schema import PersonaForm

from onair_engine.personas import (
    APART_RULE,
    DRAFT_RULES,
    EXAMPLE_ENERGY,
    EXAMPLE_SITUATIONS,
    TONE_DETAIL_RULE,
    PersonaDrafter,
    build_prompt,
    vague_detail,
)
from onair_engine.pipeline.llm import DummyLlmClient
from onair_engine.pipeline.safety import SafetyChecker
from test_api import RULES


def _form(**kw) -> PersonaForm:
    return PersonaForm(**{"formality": "casual", "energy": "high", "humor": "often", "voice": "kore", **kw})


def test_prompt_asks_for_a_way_of_talking_not_a_mood():
    user = build_prompt(_form(), 3, []).user
    assert TONE_DETAIL_RULE in user and APART_RULE in user and all(rule in user for rule in DRAFT_RULES)
    assert "말투 묘사 한 문장," not in user, "예전 문구는 남기지 않는다"
    assert "말하는 방식으로 기억되는 사람" in build_prompt(_form(), 3, []).system


def test_examples_cover_different_situations_in_order():
    user = build_prompt(_form(), 3, []).user
    positions = [user.index(f"{n}) {s}") for n, s in enumerate(EXAMPLE_SITUATIONS, 1)]
    assert positions == sorted(positions) and len(EXAMPLE_SITUATIONS) == 5
    assert "받았다는 말로 시작하지 않고" in EXAMPLE_SITUATIONS[1]


def test_examples_are_held_to_the_script_rules():
    """대본에서 막는 것을 예시가 가르치면 안 된다 (#374, #376)."""
    rules = " ".join(DRAFT_RULES)
    assert "위로하거나 다독이는 문장은 다섯 개 가운데 하나까지" in rules
    assert "농담하는 문장의 주어는 사람" in rules and "시처럼 꾸미는 말" in rules
    assert "구호나 표어, 위로하는 말, 쉬어 가자거나 느리게 하자고 권하는 말은 입버릇으로 만들지 않는다" in rules
    assert "'~하는 방송' 꼴로 끝내지 않는다" in rules


def test_examples_carry_the_energy_of_the_dj():
    """구호와 위로를 막자 텐션 높은 DJ의 예시에서 느낌표가 32% → 17%로 빠졌다."""
    for energy in ("high", "mid", "low"):
        user = build_prompt(_form(energy=energy), 3, []).user
        assert f"- examples의 에너지: {EXAMPLE_ENERGY[energy]}" in user
        assert all(EXAMPLE_ENERGY[other] not in user for other in EXAMPLE_ENERGY if other != energy)
    # "서두르지 않는"이라고 썼더니 입버릇 "천천히 볼게요"가 다섯 명에게 생겼다
    assert not any(word in EXAMPLE_ENERGY["low"] for word in ("천천히", "서두르", "느린", "느리게"))


def test_tone_detail_is_plain_behaviour():
    """사용자가 짚은 묘사 — 빗댄 말과 분위기 낱말은 코드로도 거른다."""
    for flagged in ("빗방울을 세듯 천천히 말을 이어가", "책장을 넘기듯 여백을 두며 작은 농담을 건넨다",
                    "골목 모퉁이처럼 살짝만 드러낸다", "친한 언니 같은 말투로 안부를 묻는다", "맞장구를 빠르게 얹는다"):
        assert vague_detail(flagged), flagged
    for plain in ("선택지를 숫자로 세어 좁히고, 자기 실수도 바로 인정하며 웃는다.",
                  "청취자를 같은 편으로 끌어들여 같이 호들갑 떨어.", "청취자와 같이 웃고 늘 같은 순서로 묻는다.",
                  "문장을 반듯하게 끊고 결론부터 말한다."):
        assert vague_detail(plain) == [], plain
    assert "비유는 쓰지 않는다" in TONE_DETAIL_RULE and "대본을 읽으면 그렇게 했는지 확인할 수 있는 행동" in TONE_DETAIL_RULE


def test_figurative_tone_detail_is_sent_back():
    form = _form()
    drafter = PersonaDrafter(lambda: DummyLlmClient(delay_sec=0), SafetyChecker(RULES))
    good = asyncio.run(drafter.drafts(form, 1))[0]
    item = {"dj_name": "새이름", "concept": good.concept, "examples": good.examples,
            "signature_phrases": good.signature_phrases, "forbidden": good.forbidden}
    persona, problem = drafter._finish(form, {**item, "tone_detail": "빗방울을 세듯 천천히 말을 이어가."}, set())
    assert persona is None and problem.startswith("tone_detail: 빗댄 말")
    persona, problem = drafter._finish(form, {**item, "tone_detail": "질문을 짧게 던지고 답에 크게 맞장구친다."}, set())
    assert persona is not None and problem is None


def test_host_note_comes_first():
    """위로를 줄이는 규칙이 처음에는 '따뜻하게 위로해주는 언니'라는 메모를 눌렀다."""
    with_note = build_prompt(_form(host_note="따뜻하게 위로해주는 언니 같은 느낌"), 3, []).user
    assert "위의 규칙과 부딪치면 메모가 앞선다" in with_note and "해결책부터 내미는 사람으로 만들지 않는다" in with_note
    assert "메모가 앞선다" not in build_prompt(_form(), 3, []).user


def test_dummy_drafts_still_come_through():
    """JSON 형식은 그대로다 — 화면과 저장 형식이 바뀌지 않는다."""
    drafter = PersonaDrafter(lambda: DummyLlmClient(delay_sec=0), SafetyChecker(RULES))
    drafts = asyncio.run(drafter.drafts(_form(), 3))
    assert len(drafts) == 3 and all(len(d.examples) == 5 for d in drafts)
