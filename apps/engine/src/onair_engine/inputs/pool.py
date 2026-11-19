"""인용 후보 풀 — 시간 창과 앵커 유효성으로 입력을 만료시킨다 (CORNER_DESIGN 5.1.3절)."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable

from ..domain import ABOUT_SELF, ABOUT_SONG, ABOUT_TALK, TONE_PENDING, ChatType, ListenerRequest

_MAX_ANCHOR_GAP = 1  # 반응 대상은 재생될 때 현재 또는 바로 앞 앵커여야 한다
# 무엇에 대한 말인지가 글에 없는 반응 — 대상을 가리키기만 하거나("그 말"), 짧은 감탄이다.
# 화제 낱말이 든 채팅(anchor_keyword)은 글만으로 뜻이 통해 한 건씩 인용한다
_MOOD_RULES = ("pointing", "short_reaction")
_TRACK_ANCHOR = "track:"  # 곡 소개나 곡이 나가는 동안 온 글의 앵커 (scheduler._anchor_for)


def asks(req: ListenerRequest) -> bool:
    return "?" in req.body or "？" in req.body


def leans(req: ListenerRequest) -> bool:
    """방금 들은 말이나 곡에 기대는 글 — 그 자리를 지나면 무엇에 대한 말인지 알 수 없다 (CORNER_DESIGN 5.1.5절).

    LLM이 판정했으면(inputs/chat_tone.py) 그것을 따르고, 판정이 없으면 규칙 분류의 반응형을 기대는 글로 본다.
    """
    if req.about is not None:
        return req.about != ABOUT_SELF
    return req.chat_type == ChatType.REACTION


def is_mood(req: ListenerRequest) -> bool:
    """한 건씩 받지 않고 분위기로만 전하는 채팅 (2026-11-15, 2026-11-19 결정).

    한 건씩 인용하면 멘트가 만들어져 나가는 사이 화제가 바뀌어, 앞 이야기에 대한 반응이 지금 이야기에 대한
    말처럼 들린다. 글에 내용이 없는 반응(가리키기만 하거나 짧은 감탄), 곡 감상, 기대던 말이 지나간 글(stale)이
    여기에 든다. 질문은 반응이어도 답해야 하므로 넣지 않는다.
    """
    if asks(req) or not leans(req):
        return False
    return req.stale or req.about == ABOUT_SONG or req.rule in _MOOD_RULES


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

    def answerable(self, since: float) -> list[ListenerRequest]:
        """DJ가 질문을 던진 뒤에 온 글 — 답 후보. 답은 질문에 기대는 글이라 분위기로만 전할 글도 넣는다. 곡 감상은 뺀다."""
        return [req for req in self.items
                if req.received_at >= since and req.tone != TONE_PENDING and req.about != ABOUT_SONG]

    def drop_moods(self, before: float | None = None) -> list[ListenerRequest]:
        """곡 뒤 첫 멘트가 지나갔다 — 그 자리를 놓친 반응은 받지 않는다. 뺀 글을 돌려준다.

        before를 주면 그 전에 온 반응만 뺀다.
        """
        gone = [req for req in self.items if is_mood(req) and req.tone != TONE_PENDING
                and (before is None or req.received_at < before)]
        self.items = [req for req in self.items if req not in gone]
        return gone

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
        if not leans(req):
            return now - req.received_at <= self.standalone_window_sec
        if is_mood(req):  # 무엇에 대한 반응인지 함께 전한다 — 앵커가 바뀐 뒤에 나가도 뜻이 통한다
            return now - req.received_at <= self.mood_window_sec
        if self._fresh(req, now, generating_seq):
            return True
        # 되묻는 말은 그 자리를 지나면 받지 않는다. 판정이 없는 글(규칙 분류만 있다)은 예전대로 만료시킨다
        if asks(req) or req.about is None:
            return False
        req.stale = True  # 한 건씩 받을 때를 놓쳤다 — 곡 뒤 첫 멘트가 분위기로 전한다
        return now - req.received_at <= self.mood_window_sec

    def _fresh(self, req: ListenerRequest, now: float, generating_seq: int | None) -> bool:
        """기대는 글을 한 건씩 받을 수 있는 동안 — 기대던 말이 아직 가까이 있다."""
        # 곡 소개나 곡이 나가는 동안 온 맞장구는 곡 전의 말에 대한 것이다 — 곡이 끝난 뒤에 한 건씩 받으면
        # 무엇에 대한 말인지 알 수 없다 (실행 20261119-164247: 곡을 넘겨 받은 글 131건 중 지적 29건)
        if req.about == ABOUT_TALK and not asks(req) and (req.anchor_id or "").startswith(_TRACK_ANCHOR):
            return False
        # 곡 위에서는 말하지 않으므로, 앵커 곡이 끝날 때까지는 창이 흐르지 않는다
        start = max(req.received_at, self._track_end(req.anchor_id) or req.received_at)
        if now - start > self.reaction_window_sec:
            return False
        if generating_seq is None or req.anchor_seq is None:
            return True
        return generating_seq - req.anchor_seq <= _MAX_ANCHOR_GAP
