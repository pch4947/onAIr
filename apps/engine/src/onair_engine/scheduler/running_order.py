"""러닝오더 — 코너 슬롯의 순서 있는 목록 + 현재 위치 (설계 문서 4.1).

생성 규칙(방송 시간·DJ 스타일 → 슬롯 배열)은 기획서 4.2 확정 후
RunningOrderBuilder 구현체 추가로 대응한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..domain import StationConfig


@dataclass
class Slot:
    corner_type: str


class RunningOrder:
    """끝에 도달하면 loop_from 위치부터 순환한다."""

    def __init__(self, slots: list[Slot], loop_from: int = 0):
        if not slots:
            raise ValueError("empty running order")
        self.slots = slots
        self.loop_from = loop_from
        self.position = 0

    def peek(self) -> str:
        return self.slots[self.position].corner_type

    def advance(self) -> str:
        corner_type = self.slots[self.position].corner_type
        self.position += 1
        if self.position >= len(self.slots):
            self.position = self.loop_from
        return corner_type


class RunningOrderBuilder(Protocol):
    def build(self, config: StationConfig) -> RunningOrder: ...


class FixedRunningOrderBuilder:
    """MVP: opening 1회 후 (briefing → music_intro → filler) 순환."""

    def build(self, config: StationConfig) -> RunningOrder:
        slots = [Slot("opening"), Slot("briefing"), Slot("music_intro"), Slot("filler")]
        return RunningOrder(slots, loop_from=1)
