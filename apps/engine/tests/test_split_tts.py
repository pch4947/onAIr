"""긴 대본은 나눠서 동시에 합성하고 한 파일로 붙인다 (#342).

합성에 걸리는 시간은 대본 길이에 따라 늘어난다. 2026-11-20 실제 방송에서 곡 소개 하나가 243자(약 37초)였고
제때 만들어지지 않아 앞 멘트와 약 6초 비었다. 나누면 가장 긴 조각의 시간으로 줄어든다.
"""
import asyncio
import time
import wave

import pytest

from onair_engine.audio import JOIN_GAP_MS, CachedTts, join_wavs, split_evenly
from onair_engine.pipeline.safety import split_sentences
from onair_engine.pipeline.tts import DummyTtsClient
from test_scheduler import _engine

INTRO = ("라면 자주 드시냐고 물어보신 분, 저는 얼큰한 국물이 당길 때 라면을 찾곤 해요. "
         "국물만 안 먹으면 크게 상관없다고 하던데요라고 하신 분, 국물을 덜 마시면 나트륨 섭취를 줄이는 데 도움이 되지만 "
         "면과 건더기도 함께 살펴보면 좋겠어요. "
         "음식 이야기를 재치 있게 풀어내는 랩이 건강한 식습관 수다에 유쾌한 박자를 더해줄 것 같아 직접 골랐습니다, "
         "MF DOOM - Beef Rapp (Official Audio) — MF DOOM입니다.")  # 실제 방송의 곡 소개 (243자)
SHORT = "샐러드도 취향을 담으면 심심할 틈이 없어요. 아삭한 채소를 고르고, 좋아하는 토핑을 더하면 됩니다."


class RecordingTts(DummyTtsClient):
    """부른 글과 동시에 돌던 호출 수를 적는다. fail에 든 글은 한 번 실패한다."""

    def __init__(self, delay: float = 0.05):
        self.texts: list[str] = []
        self.delay = delay
        self.running = self.peak = 0
        self.fail: set[str] = set()

    async def synthesize(self, text, out_path):
        self.texts.append(text)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(self.delay)
            if text in self.fail:
                self.fail.discard(text)
                raise RuntimeError("provider down")
            return await super().synthesize(text, out_path)
        finally:
            self.running -= 1


def _ms(path) -> int:
    with wave.open(str(path), "rb") as w:
        return round(w.getnframes() * 1000 / w.getframerate())


def test_sentences_are_grouped_evenly_and_never_broken():
    sentences = split_sentences(INTRO)
    groups = split_evenly(sentences, 130)
    assert len(groups) == 2 and " ".join(groups) == " ".join(sentences), "차례를 지키고 문장을 쪼개지 않는다"
    assert len(INTRO) == 243 and max(map(len, groups)) < len(INTRO) * 0.6, "가장 긴 조각이 전체의 절반쯤이다"
    assert split_evenly(["가" * 50, "나" * 50, "다" * 50, "라" * 50], 60) == ["가" * 50, "나" * 50, "다" * 50, "라" * 50]
    assert split_evenly(["가" * 10, "나" * 10, "다" * 100], 100) == ["가" * 10 + " " + "나" * 10, "다" * 100]
    assert split_evenly(["한 문장뿐입니다."], 5) == ["한 문장뿐입니다."] and split_evenly([], 5) == []


def test_short_script_is_synthesized_whole(tmp_path):
    inner = RecordingTts()
    tts = CachedTts(inner, split_chars=150)
    assert tts.parts(SHORT) == [SHORT]
    asyncio.run(tts.synthesize(SHORT, tmp_path / "a.wav"))
    assert inner.texts == [SHORT]


def test_long_script_is_synthesized_in_parts_at_once_and_joined(tmp_path):
    inner = RecordingTts(delay=0.2)
    tts = CachedTts(inner, pause_ms=700, split_chars=150)
    parts = tts.parts(INTRO)
    assert len(parts) == 2
    started = time.time()
    duration = asyncio.run(tts.synthesize(INTRO, tmp_path / "a.wav"))
    assert sorted(inner.texts) == sorted(parts) and inner.peak == 2, "조각을 동시에 합성한다"
    assert time.time() - started < 0.2 * 2 + 0.3, "걸리는 시간은 조각 하나의 시간이다"
    each = [max(1000, min(8000, len(part) * 90)) for part in parts]
    assert duration == _ms(tmp_path / "a.wav") == sum(each) + JOIN_GAP_MS + 700, "쉬는 시간은 맨 끝에 한 번만 붙는다"
    assert not list(tmp_path.rglob("*.tmp*")) and [p.name for p in tmp_path.iterdir()] == ["a.wav"]


def test_parts_are_cached_so_a_retry_synthesizes_only_what_failed(tmp_path):
    inner = RecordingTts()
    tts = CachedTts(inner, cache_dir=tmp_path / "cache", split_chars=150)
    first, second = tts.parts(INTRO)
    inner.fail.add(second)
    with pytest.raises(RuntimeError):
        asyncio.run(tts.synthesize(INTRO, tmp_path / "audio" / "a.wav"))
    assert not (tmp_path / "audio" / "a.wav").exists()
    asyncio.run(tts.synthesize(INTRO, tmp_path / "audio" / "a.wav"))
    assert inner.texts.count(first) == 1 and inner.texts.count(second) == 2
    asyncio.run(tts.synthesize(INTRO, tmp_path / "audio" / "b.wav"))
    assert len(inner.texts) == 3 and tts.hits == 1, "같은 대본은 전체가 캐시에서 나온다"
    assert not list(tmp_path.rglob("*.tmp*"))


def test_split_is_off_by_default_and_for_tts_that_does_not_give_wav(tmp_path):
    assert CachedTts(RecordingTts()).parts(INTRO) == [INTRO]
    mp3 = RecordingTts()
    mp3.file_ext = ".mp3"
    assert CachedTts(mp3, split_chars=150).parts(INTRO) == [INTRO]
    assert CachedTts(RecordingTts(), split_chars=150).cache_namespace != CachedTts(RecordingTts()).cache_namespace


def test_parts_that_cannot_be_joined_fall_back_to_the_whole_script(tmp_path):
    class Uneven(RecordingTts):
        """조각마다 표본 속도가 다르다."""

        async def synthesize(self, text, out_path):
            self.texts.append(text)
            rate = 16000 if len(self.texts) % 2 else 24000
            with wave.open(str(out_path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(bytes(2 * rate))
            return 1000

    inner = Uneven()
    tts = CachedTts(inner, split_chars=150)
    asyncio.run(tts.synthesize(INTRO, tmp_path / "a.wav"))
    assert inner.texts[-1] == INTRO and len(inner.texts) == 3, "이어 붙일 수 없으면 통째로 합성한다"
    assert _ms(tmp_path / "a.wav") > 0
    assert not join_wavs([tmp_path / "missing.wav"], tmp_path / "b.wav")


def test_hedge_threshold_follows_the_longest_part(tmp_path):
    engine, _ = _engine(tmp_path)
    pipeline = engine.scheduler.pipeline
    assert pipeline._longest_part(INTRO) == INTRO, "나누지 않으면 대본 전체다"
    pipeline.tts = CachedTts(DummyTtsClient(), split_chars=150)
    assert pipeline._longest_part(INTRO) == max(pipeline.tts.parts(INTRO), key=len)
