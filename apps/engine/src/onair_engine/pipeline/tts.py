"""TTS 어댑터 — 더미는 무음 wav를 생성한다. M1에서 실 어댑터 추가."""
from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import Protocol


class TtsClient(Protocol):
    async def synthesize(self, text: str, out_path: Path) -> int:
        """out_path에 오디오 파일을 쓰고 duration_ms를 반환한다."""
        ...


class DummyTtsClient:
    """텍스트 길이에 비례한 무음 wav — 관통 확인·테스트용."""

    RATE = 16000

    async def synthesize(self, text: str, out_path: Path) -> int:
        await asyncio.sleep(0.05)
        duration_ms = max(1000, min(8000, len(text) * 90))  # 대략의 한국어 발화 속도 흉내
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.RATE)
            w.writeframes(b"\x00\x00" * (self.RATE * duration_ms // 1000))
        return duration_ms


def make_tts(kind: str) -> TtsClient:
    if kind == "dummy":
        return DummyTtsClient()
    raise ValueError(f"unknown tts adapter: {kind}")
