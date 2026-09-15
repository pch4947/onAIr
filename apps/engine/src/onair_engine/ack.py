"""접수 확인(ack) 단축 경로 — 설계 문서 4.5.

ack는 생성 파이프라인을 타지 않는다. L_ack에서 엔진 기여분을 ~0으로 만들어
측정값이 편성 정책과 백엔드 경로의 효과만 반영하게 하기 위함이다.
"""
from __future__ import annotations

from pathlib import Path

from .pipeline.tts import TtsClient

# MVP 템플릿 — 요청 내용을 반영한 개인화 ack는 COULD로 미룸
ACK_TEMPLATES = [
    "사연 하나 들어왔네요. 이 곡 끝나고 읽어드릴게요.",
    "방금 신청이 도착했어요. 잠시 뒤에 소개할게요.",
    "보내주신 이야기 잘 받았어요. 곧 다뤄볼게요.",
]


class AckCache:
    """스테이션마다 DJ 보이스가 다르므로 스테이션 생성 시점에 사전 렌더링한다."""

    def __init__(self, *, tts: TtsClient, audio_dir: Path):
        self._tts = tts
        self._dir = Path(audio_dir) / "ack"
        self._entries: list[tuple[str, int]] = []  # (audio_ref, duration_ms)
        self._next = 0

    async def prerender(self) -> None:
        for i, text in enumerate(ACK_TEMPLATES):
            path = self._dir / f"ack_{i}{self._tts.file_ext}"
            duration_ms = await self._tts.synthesize(text, path)
            self._entries.append((str(path), duration_ms))

    def pick(self) -> tuple[str, int]:
        entry = self._entries[self._next % len(self._entries)]
        self._next += 1
        return entry
