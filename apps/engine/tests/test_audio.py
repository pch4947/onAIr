"""오디오 파일 관리 — 원자적 쓰기, TTS 결과 캐시, 길이 실측."""
import asyncio
import wave

import pytest

from onair_engine.audio import CachedTts
from onair_engine.pipeline.tts import DummyTtsClient


class CountingTts(DummyTtsClient):
    def __init__(self):
        self.calls = 0

    async def synthesize(self, text, out_path):
        self.calls += 1
        return await super().synthesize(text, out_path)


class FailingTts(DummyTtsClient):
    """쓰다 만 파일을 남기고 실패하는 제공자."""

    async def synthesize(self, text, out_path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"partial")
        raise RuntimeError("provider down")


def _temp_files(root):
    return list(root.rglob("*.tmp*"))


def test_cache_hit_skips_synthesis_and_measures_duration(tmp_path):
    inner = CountingTts()
    tts = CachedTts(inner, cache_dir=tmp_path / "cache")
    text = "사연 하나 들어왔네요. 이 곡 끝나고 읽어드릴게요."

    async def scenario():
        first = await tts.synthesize(text, tmp_path / "audio" / "a.wav")
        second = await tts.synthesize(text, tmp_path / "audio" / "b.wav")
        return first, second

    first, second = asyncio.run(scenario())

    assert inner.calls == 1
    assert (tts.hits, tts.misses) == (1, 1)
    assert first == second == len(text) * 90  # 무음 wav 길이를 파일에서 실측한 값
    assert (tmp_path / "audio" / "a.wav").exists()
    assert (tmp_path / "audio" / "b.wav").exists()
    assert not _temp_files(tmp_path)


def test_without_cache_synthesizes_every_time(tmp_path):
    inner = CountingTts()
    tts = CachedTts(inner)
    out = tmp_path / "audio" / "seg.wav"

    asyncio.run(tts.synthesize("짧은 멘트", out))
    asyncio.run(tts.synthesize("짧은 멘트", out))

    assert inner.calls == 2
    assert out.exists()
    assert not _temp_files(tmp_path)


def test_failed_synthesis_leaves_no_partial_file(tmp_path):
    tts = CachedTts(FailingTts(), cache_dir=tmp_path / "cache")
    out = tmp_path / "audio" / "seg.wav"

    with pytest.raises(RuntimeError, match="provider down"):
        asyncio.run(tts.synthesize("실패할 멘트", out))

    assert not out.exists(), "백엔드가 쓰다 만 파일을 보면 안 된다"
    assert not tts.cache_path("실패할 멘트").exists()
    assert not _temp_files(tmp_path)


# ── 멘트 끝 쉬는 시간 (2026-11-15 실제 방송 피드백) ───────────────────────

class Mp3Tts(DummyTtsClient):
    """WAV가 아닌 오디오를 내는 제공자."""

    file_ext = ".mp3"

    async def synthesize(self, text, out_path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"ID3not-a-wave-file")
        return 1500


def test_pause_is_appended_to_the_speech_and_counted_in_its_length(tmp_path):
    text = "오늘은 여기까지 이야기해 볼게요."
    plain = asyncio.run(CachedTts(DummyTtsClient()).synthesize(text, tmp_path / "plain.wav"))
    paused = asyncio.run(CachedTts(DummyTtsClient(), pause_ms=700).synthesize(text, tmp_path / "paused.wav"))
    assert paused == plain + 700, "방송 분량 계산이 쉬는 시간까지 센다"
    with wave.open(str(tmp_path / "paused.wav"), "rb") as audio:
        frames = audio.readframes(audio.getnframes())
        tail = audio.getframerate() * 700 // 1000 * audio.getsampwidth()
    assert set(frames[-tail:]) == {0}, "끝은 무음이다"
    assert not _temp_files(tmp_path)


def test_pause_has_its_own_cache_and_cached_audio_keeps_it(tmp_path):
    inner = CountingTts()
    text = "같은 문장입니다."
    plain = CachedTts(inner, cache_dir=tmp_path / "cache")
    paused = CachedTts(inner, cache_dir=tmp_path / "cache", pause_ms=500)

    async def scenario():
        return (await plain.synthesize(text, tmp_path / "a.wav"),
                await paused.synthesize(text, tmp_path / "b.wav"),
                await paused.synthesize(text, tmp_path / "c.wav"))

    a, b, c = asyncio.run(scenario())
    assert (b, c) == (a + 500, a + 500) and inner.calls == 2, "쉬는 시간이 다르면 캐시를 따로 쓰고, 캐시된 파일에도 붙어 있다"


def test_pause_is_skipped_for_audio_that_is_not_wav(tmp_path):
    out = tmp_path / "seg.mp3"
    duration = asyncio.run(CachedTts(Mp3Tts(), pause_ms=700).synthesize("문장", out))
    assert duration == 1500 and out.read_bytes() == b"ID3not-a-wave-file", "WAV가 아니면 손대지 않는다"
