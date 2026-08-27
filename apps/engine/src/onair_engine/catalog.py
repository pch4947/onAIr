"""음원 카탈로그 — 대본 엔진 파트가 관리한다 (확인 5 결정, 설계 문서 4.8).

MVP는 내장 더미 트랙. 백엔드의 곡 오디오 접근 방식은 1주차 계약에서 확정.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass


@dataclass
class Track:
    track_id: str
    title: str
    artist: str
    mood: str
    duration_ms: int


# TODO(M1): CC/퍼블릭 도메인 실음원 + 메타데이터 파일(무드 태그 포함) 로드로 교체
_DUMMY_TRACKS = [
    Track("cat_dummy_001", "Midnight Study", "onAIr Ensemble", "calm", 180_000),
    Track("cat_dummy_002", "Rainy Window", "onAIr Ensemble", "mellow", 200_000),
    Track("cat_dummy_003", "First Light", "onAIr Ensemble", "hopeful", 160_000),
]


class Catalog:
    def __init__(self, tracks: list[Track] | None = None):
        self._tracks = tracks if tracks is not None else list(_DUMMY_TRACKS)
        self._cycle = itertools.cycle(self._tracks) if self._tracks else None

    def pick(self) -> Track | None:
        """다음 곡 선택 — MVP는 단순 순환. TODO(M3/F-17): 무드 태그 매칭."""
        return next(self._cycle) if self._cycle else None

    def get(self, track_id: str) -> Track | None:
        return next((t for t in self._tracks if t.track_id == track_id), None)
