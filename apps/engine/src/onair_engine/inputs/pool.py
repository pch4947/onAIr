"""인용 후보 풀 — 시간 창과 앵커 유효성으로 입력을 만료시킨다 (CORNER_DESIGN 5.1.3절)."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable

from ..domain import ChatType, ListenerRequest

_MAX_ANCHOR_GAP = 1  # 반응 대상은 재생될 때 현재 또는 바로 앞 앵커여야 한다


class QuotePool:
    def __init__(self, *, reaction_window_sec: float, standalone_window_sec: float,
                 track_end: Callable[[str | None], float | None]):
        self.reaction_window_sec = reaction_window_sec
        self.standalone_window_sec = standalone_window_sec
        self._track_end = track_end
        self.items: list[ListenerRequest] = []
        self._arrivals: deque[float] = deque()

    def __len__(self) -> int:
        return len(self.items)

    def add(self, req: ListenerRequest) -> None:
        self.items.append(req)
        self._arrivals.append(req.received_at)

    def put_back(self, req: ListenerRequest) -> None:
        """생성 실패·인용 누락으로 되돌린다. 이미 기다린 입력이므로 맨 앞으로. 도착 수에는 다시 세지 않는다."""
        self.items.insert(0, req)

    def remove(self, req: ListenerRequest) -> None:
        self.items.remove(req)

    def recent_arrivals(self, now: float) -> int:
        """최근 W 동안 도착한 입력 수 — 상황(뜸함·보통·폭주) 판단 기준 (5.7절)."""
        while self._arrivals and now - self._arrivals[0] > self.reaction_window_sec:
            self._arrivals.popleft()
        return len(self._arrivals)

    def expire(self, now: float, generating_seq: int | None) -> list[ListenerRequest]:
        """창이 지났거나 앵커가 너무 멀어진 입력을 빼서 돌려준다."""
        keep, expired = [], []
        for req in self.items:
            (keep if self._alive(req, now, generating_seq) else expired).append(req)
        self.items = keep
        return expired

    def drain(self) -> list[ListenerRequest]:
        items, self.items = self.items, []
        return items

    def _alive(self, req: ListenerRequest, now: float, generating_seq: int | None) -> bool:
        if req.chat_type == ChatType.STANDALONE:
            return now - req.received_at <= self.standalone_window_sec
        # 곡 위에서는 말하지 않으므로, 앵커 곡이 끝날 때까지는 창이 흐르지 않는다
        start = max(req.received_at, self._track_end(req.anchor_id) or req.received_at)
        if now - start > self.reaction_window_sec:
            return False
        if generating_seq is None or req.anchor_seq is None:
            return True
        return generating_seq - req.anchor_seq <= _MAX_ANCHOR_GAP
