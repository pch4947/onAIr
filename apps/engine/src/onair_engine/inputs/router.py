"""입력 종류 정규화 (CORNER_DESIGN 4.1, 4.4절).

풀과 inbox는 코너가 바뀌어도 유지되므로, 입력을 지금 코너에 묶지 않고 종류별 저장소로 보낸다.
쓸 수 있는 코너에 들어갔을 때 꺼내 쓰고, 그 전까지는 기다린다 — 설계의 DEFER·HELD와 같은 효과다.
"""
from __future__ import annotations

CHAT = "chat"
MESSAGE = "message"
SONG_REQUEST = "song_request"
BOARD_STORY = "board_story"

# 계약 v1의 story·mood는 채팅이다 — 프론트 채팅창이 story로 보낸다 (5.8절)
_KINDS = {"chat": CHAT, "story": CHAT, "mood": CHAT, "message": MESSAGE,
          "song": SONG_REQUEST, "song_request": SONG_REQUEST}


def normalize_kind(kind: str) -> str:
    return _KINDS.get(kind, CHAT)
