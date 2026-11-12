"""송출 타임라인 — 지금 방송에 무엇이 나가고 있는가 (CORNER_DESIGN 5.1.3절 앵커).

엔진은 멘트를 버퍼만큼 앞서 만들지만 청취자는 듣고 있는 것에 반응한다. 그래서 채팅의 앵커는
생성 위치가 아니라 송출 위치로 정한다. 위치는 제출 순서와 길이로 추정하고, 백엔드 이벤트
(state.transition PLAYED, track.started, 계약 v2의 segment.started)가 오면 보정한다.
"""
from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import dataclass

from ..domain import Anchor


@dataclass
class _Entry:
    segment_id: str | None  # None이면 백엔드가 혼자 트는 첫 곡
    anchor: Anchor
    start: float
    speech_ms: int
    track_ref: str | None
    track_ms: int
    synced: bool = False  # 시작 시각을 백엔드 이벤트로 확인했다
    track_start: float | None = None  # track.started로 확인한 곡 시작 시각

    @property
    def song_start(self) -> float:
        return self.track_start if self.track_start is not None else self.start + self.speech_ms / 1000

    @property
    def end(self) -> float:
        return self.song_start + self.track_ms / 1000


class OnAirTimeline:
    def __init__(self) -> None:
        self._entries: list[_Entry] = []
        self._by_segment: dict[str, _Entry] = {}
        self._next_seq = itertools.count()

    def new_anchor(self, anchor_id: str, kind: str, words: Iterable[str]) -> Anchor:
        """생성 순서대로 순번을 매긴다. 앵커 유효성은 이 순번의 차이로 판단한다."""
        return Anchor(id=anchor_id, kind=kind, keywords=frozenset(words), seq=next(self._next_seq))

    def append(self, segment_id: str | None, anchor: Anchor, duration_ms: int, now: float,
               track_ref: str | None = None, track_ms: int = 0) -> None:
        """제출 순서대로 쌓는다. 앞 항목이 끝난 뒤에 재생된다고 추정한다."""
        start = max(now, self._entries[-1].end) if self._entries else now
        entry = _Entry(segment_id, anchor, start, duration_ms, track_ref, track_ms)
        self._entries.append(entry)
        if segment_id:
            self._by_segment[segment_id] = entry

    def current(self, now: float) -> Anchor | None:
        for entry in reversed(self._entries):
            if entry.start <= now:
                return entry.anchor
        return None

    def anchor_of(self, segment_id: str) -> Anchor | None:
        entry = self._by_segment.get(segment_id)
        return entry.anchor if entry else None

    def speech_ms(self, segment_id: str) -> int | None:
        entry = self._by_segment.get(segment_id)
        return entry.speech_ms if entry else None

    def sync_started(self, segment_id: str, started_at: float) -> bool:
        """세그먼트 재생 시작 확인. 처음 받은 값만 쓴다 — 재전달이면 False (계약 10.6절)."""
        entry = self._by_segment.get(segment_id)
        if entry is None or entry.synced:
            return False
        before = entry.end
        entry.start = started_at
        entry.synced = True
        self._shift_after(entry, entry.end - before)
        return True

    def remove(self, segment_id: str) -> None:
        """백엔드가 재생하지 않고 버린 세그먼트 (segment.discarded) — 뒤 항목의 추정 시각을 그만큼 당긴다."""
        entry = self._by_segment.get(segment_id)
        if entry is None or entry.synced:  # 이미 재생을 시작한 것은 버려진 것이 아니다
            return
        del self._by_segment[segment_id]
        self._shift_after(entry, entry.start - entry.end)
        self._entries.remove(entry)

    def sync_track(self, track_ref: str, started_at: float) -> None:
        """곡 재생 시작 확인 — 아직 확인하지 않은 그 곡 중 가장 앞의 것."""
        for entry in self._entries:
            if entry.track_ref == track_ref and entry.track_start is None:
                before = entry.end
                entry.track_start = started_at
                self._shift_after(entry, entry.end - before)
                return

    def track_end(self, anchor_id: str | None) -> float | None:
        """그 앵커가 곡이면 곡이 끝나는 시각. 곡이 아니거나 길이를 모르면 None."""
        for entry in reversed(self._entries):
            if entry.anchor.id == anchor_id and entry.track_ms > 0:
                return entry.end
        return None

    def _shift_after(self, entry: _Entry, delta: float) -> None:
        """확인된 시각만큼 뒤 항목의 추정치를 옮긴다. 이미 확인한 항목은 그대로 둔다."""
        for later in self._entries[self._entries.index(entry) + 1:]:
            if not later.synced:
                later.start += delta
