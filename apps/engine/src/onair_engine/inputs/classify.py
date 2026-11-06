"""채팅 유형 분류 — 반응형 / 독립형 (CORNER_DESIGN 5.1.3절).

LLM을 쓰지 않는다. 같은 입력에는 같은 결과가 나와야 선별 로그를 재현할 수 있다.
키워드는 U1의 형태소 분석 대신 토큰에서 흔한 조사를 떼어 낸 것이다 — 추가 의존성 없이 지연 0이고,
형태소 분석과의 비교는 M6에서 한다.
"""
from __future__ import annotations

import re

from ..domain import Anchor, ChatType
from .rules import ChatRules

_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")
_JOSA = ("에서는", "으로", "에서", "에게", "까지", "부터", "처럼", "이랑", "하고",
         "은", "는", "이", "가", "을", "를", "에", "의", "도", "로", "와", "과", "랑", "만", "요")
# 어느 화제에나 나오는 말 — 겹쳐도 같은 얘기를 한다는 뜻이 아니다
_STOPWORDS = frozenset({"오늘", "지금", "진짜", "너무", "정말", "그냥", "이야기", "저는", "제가"})


def _strip_josa(token: str) -> str:
    for josa in _JOSA:
        if token.endswith(josa) and len(token) - len(josa) >= 2:
            return token[:-len(josa)]
    return token


def keywords(text: str | None) -> frozenset[str]:
    if not text:
        return frozenset()
    words = (_strip_josa(t.lower()) for t in _TOKEN.findall(text))
    return frozenset(w for w in words if len(w) >= 2 and w not in _STOPWORDS)


def steer_rule(body: str, rules: ChatRules, dj_name: str) -> str | None:
    """화제를 바꿀 채팅이면 맞은 규칙(question·request·address), 아니면 None (CORNER_DESIGN 5.2절)."""
    text = body.strip()
    if len(text) < rules.steer_min_chars or not keywords(text):
        return None
    if any(p in text for p in rules.steer_questions):
        return "question"
    if any(p in text for p in rules.steer_requests):
        return "request"
    lowered = text.lower()
    if any(p.lower() in lowered for p in (*rules.steer_address, dj_name)):
        return "address"
    return None


def classify(body: str, anchor: Anchor | None, rules: ChatRules) -> tuple[ChatType, str]:
    """(유형, 맞은 규칙). 위에서부터 처음 맞는 규칙을 쓴다. 정규화 전 원문으로 판단한다."""
    text = body.strip()
    if any(p in text for p in rules.pointing):
        return ChatType.REACTION, "pointing"
    if anchor is not None and keywords(text) & anchor.keywords:
        return ChatType.REACTION, "anchor_keyword"
    if len(text) <= rules.short_reaction_chars and any(r in text for r in rules.reactions):
        return ChatType.REACTION, "short_reaction"
    return ChatType.STANDALONE, "default"
