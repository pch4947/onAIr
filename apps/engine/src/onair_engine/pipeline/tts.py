"""TTS 어댑터 — 더미는 무음 wav, edge는 실제 한국어 발화 mp3를 생성한다."""
from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import ClassVar, Protocol

DEFAULT_EDGE_VOICE = "ko-KR-SunHiNeural"


class TtsClient(Protocol):
    file_ext: ClassVar[str]  # 어댑터마다 출력 포맷이 달라 파일 확장자를 어댑터가 정한다

    async def synthesize(self, text: str, out_path: Path) -> int:
        """out_path에 오디오 파일을 쓰고 duration_ms를 반환한다."""
        ...


class DummyTtsClient:
    """텍스트 길이에 비례한 무음 wav — 관통 확인·테스트용."""

    file_ext = ".wav"
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


class EdgeTtsClient:
    """Microsoft Edge 온라인 TTS — API 키 없이 한국어 뉴럴 보이스로 대본을 실제로 읽는다.

    제공자 확정(확인 3) 전 청취 테스트용. 인터넷 연결이 필요하고 비공식 엔드포인트라
    운영 사용은 권장하지 않는다. 출력은 24kHz 48kbps mono mp3로 고정이다.
    """

    file_ext = ".mp3"
    _BYTES_PER_MS = 48_000 / 8 / 1000  # 48kbps CBR

    def __init__(self, voice: str = DEFAULT_EDGE_VOICE):
        try:
            import edge_tts
        except ImportError as exc:  # 선택 의존성 — 방송 도중이 아니라 기동 시점에 실패시킨다
            raise RuntimeError('edge TTS를 쓰려면 pip install -e ".[tts]" 가 필요합니다') from exc
        self._edge_tts = edge_tts
        self.voice = voice

    async def synthesize(self, text: str, out_path: Path) -> int:
        audio = bytearray()
        async for chunk in self._edge_tts.Communicate(text, self.voice).stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(audio)
        return round(len(audio) / self._BYTES_PER_MS)


def make_tts(kind: str, *, voice: str | None = None) -> TtsClient:
    if kind == "dummy":
        return DummyTtsClient()
    if kind == "edge":
        return EdgeTtsClient(voice or DEFAULT_EDGE_VOICE)
    raise ValueError(f"unknown tts adapter: {kind}")
