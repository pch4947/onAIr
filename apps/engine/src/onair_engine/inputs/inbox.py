"""문자 받은편지함 — text_corner가 도착 순으로 꺼내 낭독한다 (CORNER_DESIGN 5.3절)."""
from __future__ import annotations

from collections.abc import Callable

from ..domain import ListenerRequest
from .selector import readable_enough


class MessageInbox:
    def __init__(self, *, max_chars: int = 200, violates: Callable[[str], object] | None = None):
        self.max_chars = max_chars
        self._violates = violates  # L2 검사 — 걸리면 사유, 아니면 None
        self.items: list[ListenerRequest] = []
        self._read_texts: set[str] = set()  # 같은 내용은 방송에서 한 번만 읽는다
        self._read_people: set[str] = set()  # 이번 코너에서 읽은 사람

    def __len__(self) -> int:
        return len(self.items)

    def add(self, req: ListenerRequest) -> None:
        self.items.append(req)

    def begin_corner(self) -> None:
        self._read_people.clear()

    def count(self) -> int:
        """지금 읽을 수 있는 건수 — text_corner 진입 조건."""
        return len({_text(req) for req in self._candidates()})

    def take(self, n: int) -> list[dict]:
        taken = []
        for _ in range(n):
            candidates = self._candidates()
            if not candidates:
                break
            # 같은 사람 1건은 다른 사람이 있을 때만 — 혼자 보내는 방에서 영영 안 읽히면 안 된다
            req = next((c for c in candidates if c.requester_ref not in self._read_people),
                       candidates[0])
            self.items.remove(req)
            self._read_texts.add(_text(req))
            self._read_people.add(req.requester_ref)
            taken.append({"request": req, "nickname": req.nickname or "익명", "body": req.body})
        return taken

    def put_back(self, req: ListenerRequest) -> None:
        """생성 실패·낭독 누락으로 되돌린다. 이미 기다린 입력이므로 맨 앞으로."""
        self._read_texts.discard(_text(req))
        self._read_people.discard(req.requester_ref)
        self.items.insert(0, req)

    def remove(self, request_id: str) -> ListenerRequest | None:
        for req in self.items:
            if req.request_id == request_id:
                self.items.remove(req)
                return req
        return None

    def drain(self) -> list[ListenerRequest]:
        items, self.items = self.items, []
        return items

    def _candidates(self) -> list[ListenerRequest]:
        return [req for req in self.items if self._eligible(req)]

    def _eligible(self, req: ListenerRequest) -> bool:
        if len(req.body) > self.max_chars or not readable_enough(req.body):
            return False
        if _text(req) in self._read_texts:
            return False
        return self._violates is None or self._violates(req.body) is None


def _text(req: ListenerRequest) -> str:
    return " ".join(req.body.split()).lower()
