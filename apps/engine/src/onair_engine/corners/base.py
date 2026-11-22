"""모든 단계가 같이 쓰는 시스템 프롬프트 — persona 블록 (docs/persona.md 4절).

코너·단계별 지시는 user 프롬프트에만 둔다 (corners/steps.py).
"""
from __future__ import annotations

from ..domain import StationProfile
from ..prompt_data import DATA_RULES, EXAMPLES, data_block

# L1 생성단 안전 지시 — 별도 LLM 호출 없이 대본 생성 프롬프트에 내장한다 (설계 문서 4.6)
L1_GUARD = (
    "소재가 방송에 부적합하면(개인정보 노출, 욕설·혐오, 특정인 비방, 위험 행위 조장) "
    "대본 대신 정확히 REJECT 한 단어만 출력한다."
)


# 출력은 그대로 TTS에 들어가 송출된다 — 읽을 수 없는 표기는 방송 사고다
SPOKEN_RULES = (
    "출력은 그대로 음성 합성되어 송출된다. 소리 내어 읽을 문장만 쓰고, "
    "마크다운·이모지·괄호 속 지문(음악, 웃음 등)·화자 표기·따옴표로 감싸기를 쓰지 않는다. "
    "영어 약어와 숫자는 한국어로 자연스럽게 읽히게 쓴다."
)


# 발성 태그 (#369) — 2026-11-22에 사람이 자리를 골라 넣은 견본을 들었을 때 거슬리지 않았다. 자리는 규칙으로 정할 수
# 없어 대본을 쓰는 쪽이 고른다. 태그를 받는 TTS(gemini)이고 설정이 켜져 있을 때만 붙인다 (StationProfile.vocal_tags)
# 처음에는 "하나 넣을 수 있다 … 대부분의 멘트에는 넣지 않는다"고 했더니 210번 가운데 2번만 썼다(농담을 받는 멘트
# 30번 중 2번, 텐션 높은 DJ만). 웃음이 나오는 자리를 짚어 주고 "넣는다"로 바꿨다 (2026-11-22, var/vocal_check.py)
VOCAL_RULES = (
    "다만 웃음이 나오는 자리에는 <chuckle>(가볍게 웃음)이나 <laugh>(소리 내어 웃음)를 넣는다 — 청취자의 농담을 "
    "받을 때, DJ가 농담을 던진 바로 뒤가 그런 자리다. 한 번에 쓰는 글 전체에서 하나까지만 쓴다. 웃을 일이 없는 멘트, "
    "위로하는 말, 진지한 이야기, 청취자의 사연이나 고민을 전하는 대목에는 넣지 않는다. 이 두 가지 밖의 태그는 쓰지 않는다."
)


# 귀로 듣는 말의 기본기 — persona와 무관하게 모든 DJ에 공통이다
RADIO_CRAFT = (
    "라디오 멘트는 귀로 듣는 말이다. 한 문장에 한 가지 생각만 담아 짧게 끊고, "
    "청취자 한 사람에게 말하듯 쓴다. 상투적인 위로나 과장된 감탄 대신 구체적인 한마디를 고른다."
)


# 뉴스 형식 — 주관 금지를 프롬프트로도 건다. 결정적으로 지키는 것은 L2 규칙이다 (CORNER_DESIGN 6.4절)
PLAIN_RULES = "의견·평가·감정 표현 없이 사실만 전한다. 농담이나 입버릇을 쓰지 않는다."


def system_prompt(profile: StationProfile, *, plain: bool = False) -> str:
    """persona 블록 조립 — docs/persona.md 4절 순서 (정체성 → 말투·예시 → 금지 → 출력 형식 → L1).

    코너와 무관하게 같은 문자열이 나온다. plain(주관 금지 코너)이면 입버릇을 빼고 PLAIN_RULES를 붙인다.
    """
    # 첫 문장 형식은 더미 LLM의 슬롯 추출(_PROFILE_RE)이 의존한다
    # 이름을 한 번 더 따로 적는다 — "DJ 새벽다."만 있으면 이름을 '새벽다'로 읽을 때가 있다. 프롬프트에 '새벽'이
    # 시간대 낱말로도 나오자(steps.DAYTIME_RULE) 오프닝 40번 가운데 5번이 "새벽다입니다"였고, 이 문장을 넣자 0번이 됐다 (T-016)
    parts = [f"당신은 라디오 스테이션 [{profile.concept}]의 DJ {profile.dj_name}다.",
             f"DJ의 이름은 '{profile.dj_name}'이다.",
             f"말투는 {profile.tone}."]
    if plain:
        parts.append(PLAIN_RULES)
    elif profile.signature_phrases:
        phrases = ", ".join(profile.signature_phrases)
        parts.append(f"입버릇은 {phrases}. 어울릴 때만 가끔 쓰고 매 멘트마다 넣지 않는다.")
    if profile.forbidden:
        parts.append("다음은 하지 않는다: " + "; ".join(profile.forbidden) + ".")
    spoken = f"{SPOKEN_RULES} {VOCAL_RULES}" if profile.vocal_tags and not plain else SPOKEN_RULES
    parts += [RADIO_CRAFT, "대본 텍스트만 출력한다.", spoken, L1_GUARD, DATA_RULES]
    prompt = " ".join(parts)
    if profile.examples:
        # 예시는 호스트가 쓴 텍스트다 — 데이터 블록으로 넣어 지시로 읽히지 않게 한다
        examples = "\n".join(f"- {ex}" for ex in profile.examples)
        prompt += (f"\n\n이 DJ가 실제로 했던 멘트다. 말투·호흡·문장 길이만 따르고, "
                   f"문장과 내용은 그대로 가져다 쓰지 않는다.\n{data_block(EXAMPLES, examples)}")
    return prompt

