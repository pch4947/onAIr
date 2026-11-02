"""GlueBank 사전 렌더 — 병렬 합성 상한, 실패 전파, 합성 전 안전 검사."""
import asyncio
from pathlib import Path

import pytest

from onair_engine.domain import StationProfile
from onair_engine.format.loader import load_format
from onair_engine.glue import PRERENDER_CONCURRENCY, GlueBank

PROFILE = StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송")


class FakeSafety:
    def __init__(self, violation=None):
        self.violation = violation

    def check_l2(self, text):
        return self.violation


class FakeTts:
    file_ext = ".wav"

    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.running = 0
        self.peak = 0
        self.calls = []
        self.cancelled = 0

    async def synthesize(self, text: str, out_path: Path) -> int:
        self.calls.append(text)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(0.01)
            if self.fail_on is not None and len(self.calls) >= self.fail_on:
                raise RuntimeError("gemini TTS 429: quota")
            return 1000
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.running -= 1


def _bank(tmp_path, tts, safety=None):
    return GlueBank(tts=tts, audio_root=tmp_path, station_id="st_test", profile=PROFILE,
                    fmt=load_format("talk"), safety=safety or FakeSafety())


def test_prerender_renders_all_phrases_within_concurrency_limit(tmp_path):
    tts = FakeTts()
    bank = _bank(tmp_path, tts)
    asyncio.run(bank.prerender())

    texts = bank.texts()
    assert len(tts.calls) == len(texts) > PRERENDER_CONCURRENCY
    assert 1 < tts.peak <= PRERENDER_CONCURRENCY
    refs = [bank.get(*key) for key in texts]
    assert all(ref is not None and ref[1] == 1000 for ref in refs)
    assert len({ref[0] for ref in refs}) == len(texts)  # 문구마다 다른 파일


def test_prerender_failure_propagates_and_stops_the_rest(tmp_path):
    tts = FakeTts(fail_on=1)
    bank = _bank(tmp_path, tts)
    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(bank.prerender())
    assert len(tts.calls) < len(bank.texts())  # 남은 문구는 합성하지 않는다
    assert tts.running == 0


def test_prerender_checks_safety_before_any_synthesis(tmp_path):
    tts = FakeTts()
    bank = _bank(tmp_path, tts, FakeSafety(violation="금칙어"))
    with pytest.raises(ValueError, match="안전 규칙"):
        asyncio.run(bank.prerender())
    assert tts.calls == []
