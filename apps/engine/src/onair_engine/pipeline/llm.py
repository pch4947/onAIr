"""LLM 어댑터 — 제공자 미정(확인 3)이므로 더미가 기본. M1에서 실 어댑터 추가."""
from __future__ import annotations

import asyncio
from typing import Protocol

from ..domain import Prompt, Script


class LlmClient(Protocol):
    async def generate(self, prompt: Prompt) -> Script: ...


class DummyLlmClient:
    """고정 대본 반환 — API 키 없이 파이프라인 관통·테스트·발표장 폴백용."""

    def __init__(self, delay_sec: float = 0.2):
        self.delay_sec = delay_sec

    async def generate(self, prompt: Prompt) -> Script:
        await asyncio.sleep(self.delay_sec)  # 생성 지연 흉내
        first_line = prompt.user.splitlines()[0][:60]
        return Script(text=f"[더미 대본] {first_line}")


def make_llm(kind: str) -> LlmClient:
    if kind == "dummy":
        return DummyLlmClient()
    raise ValueError(f"unknown llm adapter: {kind}")  # TODO(M1): 실제 제공자 어댑터
