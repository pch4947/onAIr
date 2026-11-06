"""집계 언급·예고 트리거 — 몰린 입력을 개인이 아니라 집계로 인정한다 (CORNER_DESIGN 5.7, 9.2절).

LLM을 쓰지 않는다. 같은 입력에는 같은 결과가 나와야 로그를 재현할 수 있다.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..domain import ListenerRequest
from .classify import noun_like_keywords
from .rules import ChatRules
from .selector import readable_enough


@dataclass(frozen=True)
class ChatTrend:
    keyword: str
    people: int  # 이 키워드를 말한 사람 수
    total_people: int


def chat_trend(items: Iterable[ListenerRequest], *, now: float, window_sec: float, rules: ChatRules,
               violates: Callable[[str], object]) -> ChatTrend | None:
    """최근 W의 채팅이 충분히 많고 같은 키워드로 몰렸으면 그 키워드, 아니면 None.

    같은 내용은 한 건으로 치고 키워드는 사람 수로 센다 — 한 사람의 도배로 트렌드가 되지 않는다.
    """
    seen: set[str] = set()
    people: set[str] = set()
    by_word: dict[str, set[str]] = defaultdict(set)
    for req in items:
        text = " ".join(req.body.split()).lower()
        if (now - req.received_at > window_sec or text in seen or not readable_enough(req.body)
                or violates(req.body) is not None):
            continue
        seen.add(text)
        people.add(req.requester_ref)
        for word in noun_like_keywords(req.body, rules.aggregate_predicate_endings):
            if not _too_general(word, rules):
                by_word[word].add(req.requester_ref)
    if len(seen) < rules.flood_threshold or not by_word:
        return None
    # 사람 수가 같으면 짧은 말(명사일 가능성이 높다), 그다음 사전순 — 집합 순회 순서에 흔들리지 않게 한다
    word, who = max(by_word.items(), key=lambda pair: (len(pair[1]), -len(pair[0]), pair[0]))
    if len(who) < rules.aggregate_min_people or len(who) / len(people) < rules.aggregate_share:
        return None
    return ChatTrend(word, len(who), len(people))


def _too_general(word: str, rules: ChatRules) -> bool:
    """감탄·평가 표현은 "○○ 얘기로 뜨겁네요"의 화제가 될 수 없다 ("좋아요" → "좋아")."""
    return word in rules.aggregate_stopwords or any(word in r or r in word for r in rules.reactions)
