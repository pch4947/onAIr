"""오디오 파일 관리 — 원자적 쓰기, TTS 결과 캐시, 길이 실측."""
import asyncio

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
