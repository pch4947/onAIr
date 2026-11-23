"""이야기에서 DJ의 페르소나가 드러나게 한다 — 지시를 줄이고 말하는 방식은 DJ에게 맡긴다 (#376, T-023).

같은 DJ 설정으로 채팅 세션에서 쓴 대본은 성격이 또렷한데 엔진의 이야기 대목은 어느 DJ가 써도 같은 글이었다.
대목 72개 중 43개가 "…라고 생각해"로 끝났고, 느낌표가 든 멘트는 모든 DJ에서 0%였다.
"""
from pathlib import Path

import pytest

from onair_engine.corners.base import (
    NO_FIGURES,
    NO_POETRY,
    NO_SLANG,
    RADIO_CRAFT,
    VOICE_BY_ENERGY,
    VOICE_BY_HUMOR,
    VOICE_FIRST,
    system_prompt,
)
from onair_engine.corners.steps import (
    BLOCK_STEP,
    DJ_OPINION_RULE,
    GIST_BLOCK_RULE,
    GIST_FREE_RULE,
    NO_QUESTION_RULE,
    PLAIN_WORDS_RULE,
    SOFT_QUESTION_RULE,
    build_prompt,
)
from onair_engine.domain import Material, StationProfile
from onair_engine.inputs.topic_plan import Angle
from onair_engine.main import load_config, load_profile
from onair_engine.pipeline.repeats import MOOD_WORD_MIN_SCRIPTS, MOOD_WORDS, WornPhrases
from onair_engine.pipeline.tts import DummyTtsClient
from test_scheduler import PROFILE, _engine

ANGLE = Angle("일요일 밤 단톡방", ("월요일 싫다는 한 줄",), gist="월요일의 무게는 웃음으로 나눌 때 가벼워진다.", level=3)


def _dj(energy: str, humor: str, voice_first: bool = True) -> StationProfile:
    return StationProfile(dj_name="텐션봇", tone="밝고 경쾌한 반말", concept="오늘의 축제", energy=energy, humor=humor,
                          voice_first=voice_first)


# ── 시스템 프롬프트 ────────────────────────────────────────────────────

def test_persona_comes_first_and_the_old_restraint_is_gone():
    lively = system_prompt(_dj("high", "often"))
    assert VOICE_FIRST in lively and VOICE_BY_ENERGY["high"] in lively and VOICE_BY_HUMOR["often"] in lively
    assert RADIO_CRAFT not in lively and "과장된 감탄 대신" not in lively, "이 DJ의 감탄을 막던 지시를 뺀다"
    assert RADIO_CRAFT in system_prompt(_dj("high", "often", voice_first=False)), "끄면 예전 프롬프트 그대로다"


def test_each_kind_of_dj_gets_its_own_way_of_talking():
    prompts = {(e, h): system_prompt(_dj(e, h)) for e in ("low", "mid", "high") for h in ("rare", "some", "often")}
    assert len(set(prompts.values())) == 9
    for (energy, _), text in prompts.items():
        assert VOICE_BY_ENERGY[energy] in text


def test_calm_dj_is_kept_from_going_lyrical():
    """차분한 DJ는 말투 설명이 서정적이라, 성격을 살리라고만 하면 꾸민 표현이 24% → 54%로 되돌아갔다."""
    calm = system_prompt(_dj("low", "rare"))
    assert "꾸며서 드러내지 않고" in calm and "되풀이하지 않는다" in calm and NO_FIGURES in calm
    joking = system_prompt(_dj("low", "often"))
    assert NO_POETRY in joking and NO_FIGURES not in joking, "농담하는 DJ에게는 비유까지 막지 않는다"


def test_jokes_are_spoken_jokes_not_wordplay():
    """들은 사람이 짚은 것: "뿌듯함이 갑자기 가정용품 점검으로 번져", "물건값은 흥정해도 안전은 깎지 말기", "오완완"."""
    lively = system_prompt(_dj("high", "often"))
    assert "농담하는 문장의 주어는 사람" in lively and "낱말을 비튼 말장난" in lively
    assert "표어처럼 앞뒤 짝을 맞춘 문장은 짓지 않는다" in lively and "구호" not in lively
    assert NO_SLANG in lively
    assert "농담하는 문장의 주어" not in system_prompt(_dj("low", "rare")), "농담을 하지 않는 DJ에게는 필요 없다"


def test_signature_phrases_are_not_glued_on():
    """ "마음에 광 내자, 도시가 반짝이네." — 입버릇 둘을 이어 붙여 멘트 끝에 덧붙였다."""
    profile = _dj("mid", "often", voice_first=False)
    profile.signature_phrases = ["마음에 광 내자", "도시가 반짝이네"]
    text = system_prompt(profile)
    assert "한 번에 하나만" in text and "입버릇끼리 이어 붙이거나 멘트 끝에 덧붙이지 않는다" in text


def test_mood_words_used_again_and_again_are_called_out():
    """차분한 DJ의 멘트 151개에 "마음"이 62번 나왔다 — 어절 묶음으로는 잡히지 않아 낱말로 센다."""
    worn = WornPhrases()
    for script in ("조용히 곁에 둘게요.", "오늘은 조용히 쉬어 가요.", "마음을 조용히 살펴봐요.", "천천히 들어봐요."):
        worn.add(script)
    assert worn.worn_words() == ["조용히"], f"{MOOD_WORD_MIN_SCRIPTS}개 멘트에 나온 낱말만 알린다"
    topic = WornPhrases()
    for _ in range(4):
        topic.add("마음 이야기를 해 볼게요.", material="오늘의 주제: 마음 챙김")
    assert topic.worn_words() == [], "주제나 청취자 글에 있는 낱말은 세지 않는다"
    assert all(word in MOOD_WORDS for word in ("놓아", "머무", "천천히"))


def test_news_keeps_its_plain_voice():
    assert VOICE_FIRST not in system_prompt(_dj("high", "often"), plain=True)


# ── 이야기 덩어리 ──────────────────────────────────────────────────────

def _block(**extra) -> str:
    return build_prompt(BLOCK_STEP, Material(topic="월요병", extra={"parts": 4, "angle": ANGLE, **extra}), PROFILE).user


def test_block_leaves_the_shape_to_the_dj():
    free = _block(voice_first=True, no_question=True)
    assert "문단은 4개까지로 나누고" in free and "문단의 길이는 고르지 않아도 된다" in free
    assert GIST_FREE_RULE in free and DJ_OPINION_RULE in free and SOFT_QUESTION_RULE in free
    assert "문단마다 2~3문장" not in free and GIST_BLOCK_RULE not in free
    assert PLAIN_WORDS_RULE not in free and NO_QUESTION_RULE not in free, "꾸밈말은 시스템 프롬프트가 DJ에 맞춰 막는다"
    assert "분위기 묘사와 위로하는 말은 덩어리 전체에서 한두 문장" in free, "#374에서 줄인 것은 지킨다"


def test_block_without_the_setting_is_written_the_old_way():
    old = _block(no_question=True)
    assert "문단 4개로 나누고, 문단마다 2~3문장" in old and GIST_BLOCK_RULE in old and NO_QUESTION_RULE in old


def test_song_story_block_is_freed_too():
    extra = {"parts": 4, "song_story": {"title": "새벽 버스", "artist": "모의 밴드"}, "plain_words": True}
    assert PLAIN_WORDS_RULE in build_prompt(BLOCK_STEP, Material(topic="월요병", extra=dict(extra)), PROFILE).user
    freed = build_prompt(BLOCK_STEP, Material(topic="월요병", extra={**extra, "voice_first": True}), PROFILE).user
    assert PLAIN_WORDS_RULE not in freed and "문단의 길이는 고르지 않아도 된다" in freed


# ── 설정이 닿는 길 ─────────────────────────────────────────────────────

@pytest.mark.parametrize(("level", "expected"), [(3, True), (2, False), (0, False)])
def test_engine_turns_it_on_from_the_setting(tmp_path, level, expected):
    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    engine.settings.talk_gist = level
    engine.tts_factory = lambda voice: DummyTtsClient()
    profile = _dj("high", "often", voice_first=False)
    engine._tts_client(None, profile)  # 방송 시작과 페르소나 전환이 모두 이 길을 지난다
    engine.telemetry.close()
    assert profile.voice_first is expected


def test_persona_file_and_operating_config():
    assert (load_profile("config/personas/beongae.yaml").energy, load_profile("config/personas/beongae.yaml").humor) == ("high", "often")
    assert load_profile("config/personas/goyo.yaml").humor == "rare"
    _, settings = load_config(Path("config/station.example.yaml"))
    assert settings.talk_gist == 3
