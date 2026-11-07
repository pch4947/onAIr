"""다시 꺼낼 청취자 이야기 — 방송에 실제로 나간 입력을 나중에 다시 꺼내 흐름을 잇는다 (CORNER_DESIGN 7.7절).

청취자가 적은 방송에서 주제 토크가 할 말이 떨어질 때 쓰는 소재다. 재생 시작이 확인된(REFLECTED) 입력만 넣는다 —
L0~L2를 다 통과하고 실제로 방송된 글이다. 엔딩 돌아보기(closing_lookback)도 같은 기록을 쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..domain import ListenerRequest
from .router import BOARD_STORY, MESSAGE, SONG_REQUEST

MIN_AGE_SEC = 180.0  # [예시] 방금 한 얘기를 곧바로 되풀이하지 않는다
MAX_BODY_CHARS = 200  # 사연은 길다 — 다시 꺼낼 때는 앞부분만 넘긴다
KIND_NAMES = {MESSAGE: "문자", BOARD_STORY: "사연", SONG_REQUEST: "신청곡 한마디"}  # 나머지는 채팅
# 채팅은 닉네임을 말하지 않는다 (QUOTE_RULE) — 다시 꺼낼 때도 같다
NAMED_KINDS = (MESSAGE, BOARD_STORY, SONG_REQUEST)


@dataclass
class Aired:
    kind: str
    body: str
    nickname: str | None
    aired_at: float
    used: bool = False

    def text(self) -> str:
        head = f"종류: {KIND_NAMES.get(self.kind, '채팅')}"
        if self.nickname and self.kind in NAMED_KINDS:
            head += f"\n닉네임: {self.nickname}"
        return f"{head}\n{self.body[:MAX_BODY_CHARS]}"


class CallbackStore:
    def __init__(self, min_age_sec: float = MIN_AGE_SEC):
        self.min_age_sec = min_age_sec
        self._aired: list[Aired] = []

    def __len__(self) -> int:
        return len(self._aired)

    def add(self, req: ListenerRequest, aired_at: float) -> None:
        if req.body and req.body.strip():  # 한마디 없는 신청곡은 다시 꺼낼 말이 없다
            self._aired.append(Aired(req.kind, req.body.strip(), req.nickname, aired_at))

    def take(self, now: float) -> Aired | None:
        """방송된 지 min_age_sec가 지났고 아직 다시 꺼내지 않은 것 중 가장 먼저 나간 것. 한 항목은 한 번만."""
        for item in self._aired:
            if not item.used and now - item.aired_at >= self.min_age_sec:
                item.used = True
                return item
        return None

    def lookback(self, n: int = 3) -> list[Aired]:
        """엔딩 돌아보기 소재 — 오늘 나간 것 중 처음·가운데·마지막처럼 고르게 n개."""
        if len(self._aired) <= n:
            return list(self._aired)
        step = (len(self._aired) - 1) / (n - 1)
        return [self._aired[round(i * step)] for i in range(n)]
