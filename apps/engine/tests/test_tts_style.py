"""TTS에 말투 지시와 쉼 태그를 보낸다 (#369).

2026-11-22에 같은 멘트를 네 가지로 합성해 듣고 정했다 — 스타일과 태그를 넣은 쪽이 낫고, "서두르지 않고" 같은
말은 말을 너무 느리게 만든다.
"""
import asyncio

import pytest

from onair_engine.domain import StationProfile
from onair_engine.main import load_profile
from onair_engine.pipeline.tts import (
    PAUSE_TAG,
    VOICE_PACE,
    DummyTtsClient,
    GeminiTtsClient,
    voice_style,
    with_pauses,
)
from test_scheduler import _engine
from test_tts_gemini import _audio_response, _serve, _wav_bytes

MENT = "모의 밴드의 새벽 버스 듣고 왔습니다. 밤에 듣기 좋다는 반응도 있었어요. 붕어빵은 팥인가요? 저는 팥이에요."


# ── 쉼 태그 ────────────────────────────────────────────────────────────

def test_pauses_go_between_sentences():
    assert with_pauses(MENT) == (f"모의 밴드의 새벽 버스 듣고 왔습니다. {PAUSE_TAG} 밤에 듣기 좋다는 반응도 있었어요. "
                                 f"{PAUSE_TAG} 붕어빵은 팥인가요? {PAUSE_TAG} 저는 팥이에요.")
    assert with_pauses("한 문장뿐입니다.") == "한 문장뿐입니다."
    assert with_pauses("소수점 3.5는 문장 끝이 아니에요. 맞죠?").count(PAUSE_TAG) == 1


def test_only_the_first_few_gaps_get_a_pause():
    text = " ".join(f"{n}번째 문장입니다." for n in range(1, 9))
    marked = with_pauses(text, limit=4)
    assert marked.count(PAUSE_TAG) == 4 and marked.replace(f" {PAUSE_TAG}", "") == text


# ── 말투 지시 ──────────────────────────────────────────────────────────

def test_style_comes_from_the_energy_the_host_picked():
    assert voice_style("low") == f"calm and warm, {VOICE_PACE}"
    assert len({voice_style(e) for e in ("low", "mid", "high")}) == 3
    assert voice_style("모름") == voice_style("mid")
    assert voice_style("low", {"low": "soft and steady"}) == f"soft and steady, {VOICE_PACE}"
    # 빠르기를 늦추는 말은 넣지 않는다 — "서두르지 않고"는 같은 글을 41% 길게 만들었다
    assert all("slow" not in voice_style(e) for e in ("low", "mid", "high"))


def test_request_carries_the_style_and_the_pauses(tmp_path):
    base_url, received, server = _serve([(200, _audio_response(_wav_bytes(500)))] * 2)
    try:
        styled = GeminiTtsClient("kore", api_key="k", base_url=base_url, style=voice_style("low"), pauses=True)
        asyncio.run(styled.synthesize(MENT, tmp_path / "a.wav"))
        plain = GeminiTtsClient("kore", api_key="k", base_url=base_url)
        asyncio.run(plain.synthesize(MENT, tmp_path / "b.wav"))
    finally:
        server.shutdown()
    item = received[0]["json"]["input"][0]["content"][0]
    assert item["text"] == with_pauses(MENT)
    assert item["annotations"] == [{"type": "speech_metadata", "style": voice_style("low")}]
    assert received[1]["json"]["input"][0]["content"][0] == {"type": "text", "text": MENT}, "끄면 예전 요청 그대로다"


def test_cache_is_kept_apart_by_style_and_pauses():
    plain = GeminiTtsClient("kore", api_key="k")
    names = {plain.cache_namespace,
             GeminiTtsClient("kore", api_key="k", style=voice_style("low")).cache_namespace,
             GeminiTtsClient("kore", api_key="k", style=voice_style("high")).cache_namespace,
             GeminiTtsClient("kore", api_key="k", style=voice_style("low"), pauses=True).cache_namespace,
             GeminiTtsClient("kore", api_key="k", pauses=True).cache_namespace}
    assert len(names) == 5
    assert plain.cache_namespace == "gemini/gemini-3.8-flash-tts/kore", "끄면 예전 캐시를 그대로 쓴다"
    assert all("\n" not in n and " " not in n for n in names), "캐시 이름은 폴더 이름이 된다"


# ── 엔진이 DJ마다 건다 ─────────────────────────────────────────────────

class StyledTts(DummyTtsClient):
    def __init__(self):
        self.calls = []

    def use_style(self, style, *, pauses=False):
        self.calls.append((style, pauses))


@pytest.mark.parametrize(("style_on", "pauses_on", "expected"), [
    (True, True, [(voice_style("high"), True)]),
    (True, False, [(voice_style("high"), False)]),
    (False, True, [(None, True)]),
    (False, False, []),
])
def test_engine_sets_the_style_for_this_dj(tmp_path, style_on, pauses_on, expected):
    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    engine.settings.tts_style, engine.settings.tts_pauses = style_on, pauses_on
    client = StyledTts()
    engine.tts_factory = lambda voice: client
    profile = StationProfile(dj_name="번개", tone="텐션 높은 반말", concept="불금 수다", energy="high")
    assert engine._tts_client("kore", profile) is client
    engine.telemetry.close()
    assert client.calls == expected


def test_tts_that_cannot_take_a_style_is_left_alone(tmp_path):
    """다른 TTS는 태그를 글자로 읽는다 — 말투 지시를 받을 수 있는 TTS에만 건다."""
    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    engine.settings.tts_style = engine.settings.tts_pauses = True
    plain = DummyTtsClient()
    engine.tts_factory = lambda voice: plain
    assert engine._tts_client(None, StationProfile(dj_name="새벽", tone="차분한 존댓말", concept="심야")) is plain
    engine.telemetry.close()


def test_persona_file_carries_the_energy():
    assert load_profile("config/personas/beongae.yaml").energy == "high"
    assert load_profile({"dj_name": "새벽", "tone": "차분한 존댓말", "concept": "심야"}).energy == "mid"
