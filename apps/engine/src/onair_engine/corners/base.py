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


# 페르소나를 앞세우는 말하기 (설정 talk_gist 3, #376, T-023). 같은 DJ 설정으로 채팅 세션에서 쓴 대본은 성격이 또렷한데
# 엔진의 이야기 대목은 어느 DJ가 써도 같은 글이었다. 원인은 지시가 페르소나를 누른 것이다 — "과장된 감탄 대신
# 구체적인 한마디", "청취자에게 질문하지 않는다", "평범한 낱말로", 그리고 문단 넷에 2~3문장이라는 하나뿐인 틀.
# 본보기 문장은 적지 않는다(적으면 모든 DJ가 그 문장을 쓴다, T-007) — 말하는 방식의 종류만 적는다
RADIO_CRAFT_FREE = "라디오 멘트는 귀로 듣는 말이다. 청취자 한 사람에게 말하듯 쓴다."
VOICE_FIRST = ("이 DJ가 마이크 앞에서 실제로 하는 말로 쓴다. 글로 쓴 수필이 아니다. 위의 말투와 아래 말투 예시에 드러난 "
               "성격이 문장마다 들려야 한다 — 어느 DJ가 써도 같을 문장은 쓰지 않는다.")
VOICE_BY_ENERGY = {
    "high": ("텐션이 높은 DJ다. 짧게 끊어 치는 말과 감탄으로 분위기를 끌어올리고, 청취자에게 바로 말을 건다 — "
             "같이 해 보자고 시키기, 몇 가지로 꼽기, 같이 외칠 구호 같은 방식을 쓴다."),
    "mid": "편하게 수다 떠는 DJ다. 혼잣말처럼 던지는 말, 맞장구, 자기 얘기를 섞어 말한다.",
    # 처음에는 "곁에 앉은 사람에게 말하듯"까지만 적었다. 차분한 DJ는 말투 설명이 서정적이라("빗소리와 노래 사이에
    # 위로를 놓아드려요") 성격을 살리라고 하자 꾸민 표현이 24% → 54%로 되돌아갔다 (실행 20261123-130303)
    "low": ("차분한 DJ다. 서두르지 않고 낮은 목소리로, 곁에 앉은 사람에게 말하듯 한다. 차분함은 말의 속도와 온도로 "
            "드러낸다 — 풍경을 그리거나 말을 꾸며서 드러내지 않는다."),
}
VOICE_BY_HUMOR = {
    # "웃기려는 과장과 엉뚱한 비교는 좋다"고만 했을 때는 "혼자 들면 덤벨인데 같이 들면 풍선"처럼 지어낸 말장난이 늘었다 —
    # 고른 사람이 싫어한 "주판도 얼큰하게 돌아가네"와 같은 종류다. 입으로 하는 농담으로 좁힌다
    "often": ("농담을 아끼지 않는다. 친구끼리 말로 주고받는 식의 농담과 과장이다 — 글에서나 쓸 법한 "
              "멋 부린 말장난이나 억지 비유는 하지 않는다."),
    "some": "가벼운 농담을 가끔 섞는다.",
    "rare": "",
}
# 시처럼 꾸민 말은 고른 사람들이 가장 자주 싫어했다(T-022). 웃기려는 과장은 그것과 다르다 — 농담을 거의 하지 않는
# DJ에게는 비유까지 막고, 농담하는 DJ에게는 시처럼 꾸민 말만 막는다
NO_POETRY = "풍경이나 감정을 시처럼 꾸미는 말은 쓰지 않는다. 뜻이 바로 들리는 평범한 낱말로 말한다."
NO_FIGURES = ("사물이나 상황을 다른 것에 빗대는 비유와 시처럼 꾸민 표현은 쓰지 않는다. "
              "뜻이 바로 들리는 평범한 낱말로 말한다.")


def voice_first_rules(profile: StationProfile) -> str:
    parts = [VOICE_FIRST, VOICE_BY_ENERGY.get(profile.energy, VOICE_BY_ENERGY["mid"]),
             VOICE_BY_HUMOR.get(profile.humor, ""), NO_FIGURES if profile.humor == "rare" else NO_POETRY]
    return " ".join(p for p in parts if p)


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
    craft = f"{RADIO_CRAFT_FREE} {voice_first_rules(profile)}" if profile.voice_first and not plain else RADIO_CRAFT
    parts += [craft, "대본 텍스트만 출력한다.", spoken, L1_GUARD, DATA_RULES]
    prompt = " ".join(parts)
    if profile.examples:
        # 예시는 호스트가 쓴 텍스트다 — 데이터 블록으로 넣어 지시로 읽히지 않게 한다
        examples = "\n".join(f"- {ex}" for ex in profile.examples)
        prompt += (f"\n\n이 DJ가 실제로 했던 멘트다. 말투·호흡·문장 길이만 따르고, "
                   f"문장과 내용은 그대로 가져다 쓰지 않는다.\n{data_block(EXAMPLES, examples)}")
    return prompt

