"""발성 태그 — 대본을 쓰는 LLM이 웃을 자리에 <laugh>·<chuckle>을 넣고, 합성에만 쓴다 (#369).

화면·안전 검사·인용 검사·기록은 태그 없는 대본을 본다. 태그를 받지 못하는 TTS에는 태그 없는 글을 보낸다.
"""
import asyncio
import sqlite3

import pytest

from onair_engine.audio import CachedTts
from onair_engine.corners.base import VOCAL_RULES, system_prompt
from onair_engine.domain import GenerationJob, Material, Script, StationProfile
from onair_engine.pipeline.llm import LlmError, clean_script, strip_vocal, vocal_script
from onair_engine.pipeline.tts import DummyTtsClient, GeminiTtsClient
from test_scheduler import _engine

TAGGED = "운동복이 침대 위에서 휴식 담당이 되는 거지. <laugh> 그 전에 양말만 신어 보자."
PLAIN = "운동복이 침대 위에서 휴식 담당이 되는 거지. 그 전에 양말만 신어 보자."


# ── 대본 다듬기 ────────────────────────────────────────────────────────

def test_script_keeps_a_plain_text_and_a_spoken_text():
    script = vocal_script(TAGGED)
    assert script.text == PLAIN and script.spoken == TAGGED
    assert vocal_script(PLAIN).spoken is None, "태그가 없으면 합성용 글을 따로 두지 않는다"
    assert strip_vocal(TAGGED) == PLAIN


def test_only_allowed_tags_survive_and_only_one():
    script = vocal_script("<chuckle> 그건 좀 웃기네요. <laugh> 그렇죠? <sigh> 휴. <applause>")
    assert script.text == "그건 좀 웃기네요. 그렇죠? 휴."
    assert script.spoken == "<chuckle> 그건 좀 웃기네요. 그렇죠? 휴.", "허용한 태그 하나만 남긴다"
    assert vocal_script("반가워요 < Laugh > 정말요").spoken == "반가워요 <laugh> 정말요"


def test_unknown_tags_never_reach_the_script():
    """지우지 않으면 '>'만 떨어져 '<sigh'가 대본에 남아 그대로 읽힌다."""
    assert clean_script("오늘은 좀 피곤하네요 <sigh> 그래도 가 볼게요.") == "오늘은 좀 피곤하네요 그래도 가 볼게요."
    assert clean_script(TAGGED) == PLAIN, "따로 청하지 않으면 허용 태그도 남기지 않는다"
    with pytest.raises(LlmError):
        clean_script("<laugh>", keep_vocal=True)


# ── 프롬프트 ───────────────────────────────────────────────────────────

def test_prompt_allows_tags_only_when_the_tts_can_voice_them():
    profile = StationProfile(dj_name="도도", tone="장난스러운 반말", concept="주말 수다")
    assert VOCAL_RULES not in system_prompt(profile)
    profile.vocal_tags = True
    assert VOCAL_RULES in system_prompt(profile)
    assert VOCAL_RULES not in system_prompt(profile, plain=True), "뉴스처럼 주관을 끄는 코너에는 쓰지 않는다"


@pytest.mark.parametrize(("setting", "client", "expected"), [
    (True, "gemini", True), (False, "gemini", False), (True, "dummy", False)])
def test_engine_turns_tags_on_for_a_tts_that_takes_them(tmp_path, setting, client, expected):
    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    engine.settings.tts_vocal_tags = setting
    made = GeminiTtsClient("kore", api_key="k") if client == "gemini" else DummyTtsClient()
    engine.tts_factory = lambda voice: made
    profile = StationProfile(dj_name="도도", tone="장난스러운 반말", concept="주말 수다")
    engine._tts_client("kore", profile)
    engine.telemetry.close()
    assert profile.vocal_tags is expected
    assert CachedTts(made, cache_dir=None).vocal_tags is (client == "gemini")


# ── 합성에만 쓴다 ──────────────────────────────────────────────────────

class Says:
    def __init__(self, raw):
        self.raw = raw

    async def generate(self, prompt):
        return vocal_script(self.raw)

    async def complete(self, prompt, *, max_tokens):
        return self.raw


class Hears(DummyTtsClient):
    def __init__(self, vocal_tags):
        self.vocal_tags = vocal_tags
        self.heard = []

    async def synthesize(self, text, out_path):
        self.heard.append(text)
        return await super().synthesize(text, out_path)


@pytest.mark.parametrize(("takes_tags", "heard"), [(True, TAGGED), (False, PLAIN)])
def test_tts_gets_the_tags_and_everything_else_gets_plain_text(tmp_path, takes_tags, heard):
    engine, _ = _engine(tmp_path)
    pipeline = engine.scheduler.pipeline
    pipeline.llm, pipeline.tts = Says(TAGGED), Hears(takes_tags)
    job = GenerationJob(job_id="j", corner_type="topic_talk", step="talk", material=Material(topic="운동"))
    segment = asyncio.run(pipeline.run(job))
    engine.telemetry.close()
    assert pipeline.tts.heard == [heard]
    assert pipeline._pending_air[segment.id] == PLAIN, "방송 기록과 화면의 대본에는 태그가 없다"
    assert list(pipeline._recent)[-1] == PLAIN, "다음 멘트가 이어 쓰는 맥락에도 태그가 없다"
    stages = [s for (s,) in sqlite3.connect(tmp_path / "metrics.sqlite").execute(
        "SELECT result FROM generation_log WHERE stage = 'vocal'")]
    assert stages == (["laugh"] if takes_tags else []), "태그를 소리로 낸 멘트만 기록에 남는다"


def test_block_keeps_one_tag_for_the_whole_block(tmp_path):
    engine, _ = _engine(tmp_path)
    pipeline = engine.scheduler.pipeline
    raw = "첫 문단이에요. <chuckle> 웃기죠.\n\n둘째 문단이에요. <laugh> 이것도요.\n\n셋째 문단이에요. 끝."
    pipeline.llm = Says(raw)
    from onair_engine.domain import Prompt
    texts = asyncio.run(pipeline._write_block(Prompt(system="", user=""), 3))
    engine.telemetry.close()
    assert texts == ["첫 문단이에요. 웃기죠.", "둘째 문단이에요. 이것도요.", "셋째 문단이에요. 끝."]
    assert pipeline._vocal == {"첫 문단이에요. 웃기죠.": "첫 문단이에요. <chuckle> 웃기죠."}


def test_rewritten_script_is_voiced_without_the_tag(tmp_path):
    """대본을 고쳐 쓴 뒤(문장 빼기 등)에는 맞는 합성용 글이 없다 — 태그 없이 합성한다."""
    engine, _ = _engine(tmp_path)
    pipeline = engine.scheduler.pipeline
    pipeline.tts = Hears(True)
    pipeline._keep_vocal(PLAIN, TAGGED)
    job = GenerationJob(job_id="j", corner_type="topic_talk", step="talk", material=Material())
    asyncio.run(pipeline._voice(job, Script(text="그 전에 양말만 신어 보자."), 0.0, None))
    engine.telemetry.close()
    assert pipeline.tts.heard == ["그 전에 양말만 신어 보자."]


def test_a_second_laugh_soon_after_is_not_voiced(tmp_path):
    """대본은 농담을 받을 때마다 웃음을 넣는다 — 연달아 웃으면 틀이 되므로 간격을 둔다."""
    engine, _ = _engine(tmp_path)
    pipeline = engine.scheduler.pipeline
    pipeline.tts = Hears(True)
    job = GenerationJob(job_id="j", corner_type="topic_talk", step="talk", material=Material())
    asyncio.run(pipeline._voice(job, Script(text=PLAIN, spoken=TAGGED), 0.0, None))
    asyncio.run(pipeline._voice(job, Script(text=PLAIN, spoken=TAGGED), 0.0, None))
    pipeline._last_vocal_at -= 1000  # 간격이 지났다
    asyncio.run(pipeline._voice(job, Script(text=PLAIN, spoken=TAGGED), 0.0, None))
    engine.telemetry.close()
    assert pipeline.tts.heard == [TAGGED, PLAIN, TAGGED]
