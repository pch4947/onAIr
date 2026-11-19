"""인용 후보 풀 — 시간 창과 앵커 유효성으로 입력을 만료시킨다 (CORNER_DESIGN 5.1.3절)."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable

from ..domain import TONE_PENDING, ChatType, ListenerRequest

_MAX_ANCHOR_GAP = 1  # 반응 대상은 재생될 때 현재 또는 바로 앞 앵커여야 한다
# 무엇에 대한 말인지가 글에 없는 반응 — 대상을 가리키기만 하거나("그 말"), 짧은 감탄이다.
# 화제 낱말이 든 채팅(anchor_keyword)은 글만으로 뜻이 통해 한 건씩 인용한다
_MOOD_RULES = ("pointing", "short_reaction")


def is_mood(req: ListenerRequest) -> bool:
    """분위기로만 전하는 채팅 — 그때 나가던 말이나 곡에 대한 반응이다 (2026-11-15 결정).

    한 건씩 인용하면 멘트가 만들어져 나가는 사이 화제가 바뀌어, 앞 이야기에 대한 반응이 지금 이야기에 대한
    말처럼 들린다. 질문은 반응이어도 답해야 하므로 한 건씩 인용한다.
    """
    return (req.chat_type == ChatType.REACTION and req.rule in _MOOD_RULES
            and "?" not in req.body and "？" not in req.body)


class QuotePool:
    def __init__(self, *, reaction_window_sec: float, standalone_window_sec: float,
                 track_end: Callable[[str | None], float | None], mood_window_sec: float = 300.0):
        self.reaction_window_sec = reaction_window_sec
        self.standalone_window_sec = standalone_window_sec
        self.mood_window_sec = mood_window_sec
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

    def pop(self, request_id: str) -> ListenerRequest | None:
        for req in self.items:
            if req.request_id == request_id:
                self.items.remove(req)
                return req
        return None

    def quotable(self) -> list[ListenerRequest]:
        """한 건씩 인용할 수 있는 채팅. 성격 판정을 기다리는 채팅은 아직 후보가 아니다 (inputs/chat_tone.py)."""
        return [req for req in self.items if not is_mood(req) and req.tone != TONE_PENDING]

    def reactions(self) -> list[ListenerRequest]:
        """분위기로만 전하는 반응."""
        return [req for req in self.items if is_mood(req) and req.tone != TONE_PENDING]

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
        if is_mood(req):  # 무엇에 대한 반응인지 함께 전한다 — 앵커가 바뀐 뒤에 나가도 뜻이 통한다
            return now - req.received_at <= self.mood_window_sec
        # 곡 위에서는 말하지 않으므로, 앵커 곡이 끝날 때까지는 창이 흐르지 않는다
        start = max(req.received_at, self._track_end(req.anchor_id) or req.received_at)
        if now - start > self.reaction_window_sec:
            return False
        if generating_seq is None or req.anchor_seq is None:
            return True
        return generating_seq - req.anchor_seq <= _MAX_ANCHOR_GAP
