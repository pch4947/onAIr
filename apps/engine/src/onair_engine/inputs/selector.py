"""QuoteSelector — 멘트에 넣을 채팅을 규칙으로 고른다 (CORNER_DESIGN 5.1, 5.7절).

고르는 순서는 선별 기준만 정한다. 유형은 창과 후보 자격에만 쓰고 점수에 넣지 않는다 —
분류와 관련성 점수가 같은 키워드 겹침을 쓰므로, 점수에도 넣으면 "최근성 우선" 조건까지
관련성 우선처럼 동작해 RQ-A의 조건 차이가 흐려진다.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from ..domain import ChatType, ListenerRequest
from .classify import keywords

_READABLE = re.compile(r"[0-9A-Za-z가-힣]")
_MIN_READABLE_CHARS = 2  # 'ㅋㅋㅋ'만 있는 채팅은 말로 인용할 내용이 없다

RECENCY = "recency"
RELEVANCE = "relevance"
DIVERSITY = "diversity"
CRITERIA = (RECENCY, RELEVANCE, DIVERSITY)


@dataclass
class Selection:
    situation: str  # quiet | normal | flood
    criterion: str
    arrivals: int
    picked: list[ListenerRequest] = field(default_factory=list)
    # 후보·점수 전체 — RQ-A 분석의 원천이다
    candidates: list[dict] = field(default_factory=list)


class QuoteSelector:
    def __init__(self, criterion: str, *, flood_threshold: int, max_quote_chars: int,
                 violates: Callable[[str], object]):
        if criterion not in CRITERIA:
            raise ValueError(f"알 수 없는 선별 기준: {criterion}")
        self.criterion = criterion
        self.flood_threshold = flood_threshold
        self.max_quote_chars = max_quote_chars
        self._violates = violates  # L2 검사 — 걸리면 사유, 아니면 None
        self._quoted_counts: dict[str, int] = {}

    def situation(self, arrivals: int) -> str:
        if arrivals == 0:
            return "quiet"
        return "flood" if arrivals >= self.flood_threshold else "normal"

    def select(self, items: Iterable[ListenerRequest], *, limit: int, arrivals: int,
               last_requester: str | None, topic_words: frozenset[str]) -> Selection:
        situation = self.situation(arrivals)
        scored = []
        for req in items:
            if not self._eligible(req, situation):
                continue
            scored.append((self._score(req, topic_words), req))
        # 연속 금지는 다른 사람이 있을 때만 — 혼자 채팅하는 방에서 영영 인용되지 않으면 안 된다
        scored = [pair for pair in scored if pair[1].requester_ref != last_requester] or scored
        scored.sort(key=lambda pair: pair[0], reverse=True)

        picked: list[ListenerRequest] = []
        seen: set[str] = set()
        for _, req in scored:
            text = " ".join(req.body.split()).lower()
            if text in seen:  # 같은 내용은 한 건으로 친다
                continue
            seen.add(text)
            if len(picked) < limit:
                picked.append(req)
        for req in picked:
            self._quoted_counts[req.requester_ref] = self._quoted_counts.get(req.requester_ref, 0) + 1
        return Selection(
            situation=situation, criterion=self.criterion, arrivals=arrivals, picked=picked,
            candidates=[{"request_id": req.request_id, "chat_type": str(req.chat_type),
                         "score": list(score)} for score, req in scored],
        )

    def forget(self, req: ListenerRequest) -> None:
        """인용이 실제로 나가지 않았다 — 다양성 점수에서 뺀다."""
        count = self._quoted_counts.get(req.requester_ref, 0)
        if count > 0:
            self._quoted_counts[req.requester_ref] = count - 1

    def _eligible(self, req: ListenerRequest, situation: str) -> bool:
        if situation == "flood" and req.chat_type != ChatType.REACTION:
            return False
        if len(req.body) > self.max_quote_chars:
            return False
        if len(_READABLE.findall(req.body)) < _MIN_READABLE_CHARS:
            return False
        return self._violates(req.body) is None

    def _score(self, req: ListenerRequest, topic_words: frozenset[str]) -> tuple[float, ...]:
        if self.criterion == RELEVANCE:
            return (len(keywords(req.body) & topic_words), req.received_at)
        if self.criterion == DIVERSITY:
            return (-self._quoted_counts.get(req.requester_ref, 0), req.received_at)
        return (req.received_at,)
