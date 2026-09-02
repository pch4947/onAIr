"""SchedulingPolicy 인터페이스 — 연구 확장점 (설계 문서 4.2).

정책 A/B/C는 기획서 8장(연구 설계) 확정 후 이 인터페이스의 구현체로 추가한다.
정책은 세션(방송) 단위로 고정 주입되며 방송 중 핫스왑은 없다 (확인 8 가정).
"""
from __future__ import annotations

from typing import Protocol

from ...domain import Decision, ScheduleContext


class SchedulingPolicy(Protocol):
    name: str

    def decide(self, ctx: ScheduleContext) -> Decision: ...
