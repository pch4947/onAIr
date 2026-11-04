"""게시판 사연 풀 — 백엔드가 추린 후보 중 story_corner가 읽을 것을 규칙으로 고른다 (계약 3.5절)."""
from __future__ import annotations

from collections.abc import Callable

from ..domain import ListenerRequest
from .classify import keywords

MAX_STORIES = 30  # [예시] 계약 3.5절의 후보 상한 — 넘으면 가장 오래된 사연을 뺀다


class StoryPool:
    def __init__(self, *, topic_words: frozenset[str] = frozenset(),
                 violates: Callable[[str], object] | None = None):
        self._topic_words = topic_words
        self._violates = violates
        self.items: list[ListenerRequest] = []
        self._seen: set[str] = set()  # 재전달과 이미 읽은 사연을 다시 받지 않는다

    def __len__(self) -> int:
        return len(self.items)

    def add(self, story: ListenerRequest) -> bool:
        if story.request_id in self._seen:
            return False
        self._seen.add(story.request_id)
        self.items.append(story)
        if len(self.items) > MAX_STORIES:
            self.items.remove(min(self.items, key=lambda s: s.received_at))
        return True

    def withdraw(self, story_id: str) -> bool:
        for story in self.items:
            if story.request_id == story_id:
                self.items.remove(story)
                return True
        return False

    def count(self) -> int:
        return len(self._candidates())

    def take(self, n: int) -> list[dict]:
        ranked = sorted(self._candidates(), key=self._score, reverse=True)[:n]
        for story in ranked:
            self.items.remove(story)
        return [{"request": s, "nickname": s.nickname or "익명", "body": s.body} for s in ranked]

    def put_back(self, story: ListenerRequest) -> None:
        self.items.append(story)

    def _candidates(self) -> list[ListenerRequest]:
        if self._violates is None:
            return list(self.items)
        return [s for s in self.items if self._violates(s.body) is None]

    def _score(self, story: ListenerRequest) -> tuple[int, float]:
        words = keywords(story.body) | keywords(" ".join(story.tags))
        return (len(words & self._topic_words), story.received_at)
