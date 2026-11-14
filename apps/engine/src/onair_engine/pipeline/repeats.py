"""방송에서 이미 여러 번 쓴 표현 — 같은 구절이 멘트마다 되풀이되지 않게 한다.

직전 멘트 맥락(pipeline._recent)은 최근 몇 개만 본다. 주제를 풀어 말한 구절처럼 코너에 들어올 때마다
한 번씩 나오는 말은 그 창 밖에 있어 걸리지 않는다 — 2026-11-15 실제 방송에서 주제가 "수능 D-4 화이팅"일 때
"수능이 나흘 남은 오늘"이 방송 내내 되풀이됐다. 그래서 방송 전체에서 어절 묶음을 세어, 여러 멘트에 나온
묶음을 다음 프롬프트에 알려 준다.

LLM을 쓰지 않는다. 형태소 분석 없이 어절 3~4개 묶음을 그대로 센다 — 조사만 바꿔 되풀이한 말은 못 잡는다.
"""
from __future__ import annotations

from collections import Counter
from itertools import pairwise

_SIZES = (4, 3)  # 어절 수. 두 어절은 "것 같아요"처럼 말버릇이 아닌 문법 표현이 걸린다
_EDGE = " \t\n.,!?…~\"'“”‘’()[]·"
MIN_SCRIPTS = 2  # 서로 다른 멘트 이만큼에 나왔으면 이미 여러 번 쓴 표현이다
MAX_PHRASES = 6  # 프롬프트에 알려 주는 수 — 많으면 지시가 묻힌다


def _words(text: str) -> list[str]:
    return [w for w in (token.strip(_EDGE) for token in text.split()) if w]


def _pairs(words: list[str]) -> set[tuple[str, str]]:
    return set(pairwise(words))


def _phrases(text: str) -> set[str]:
    words = _words(text)
    return {" ".join(words[i:i + n]) for n in _SIZES for i in range(len(words) - n + 1)}


class WornPhrases:
    def __init__(self, min_scripts: int = MIN_SCRIPTS, max_phrases: int = MAX_PHRASES):
        self.min_scripts = min_scripts
        self.max_phrases = max_phrases
        self._counts: Counter[str] = Counter()
        self._last_seen: dict[str, int] = {}
        self._scripts = 0

    def clear(self) -> None:
        self._counts.clear()
        self._last_seen.clear()
        self._scripts = 0

    def add(self, script: str, material: str = "") -> None:
        """대본 하나를 센다. 소재(프롬프트의 곡 제목·청취자 글·주제)에 있는 말이 든 묶음은 세지 않는다 —
        DJ의 말버릇이 아니라 말해야 하는 내용이다. 소재와 이어진 두 어절이 겹치면 소재의 말로 본다."""
        self._scripts += 1
        given = _pairs(_words(material))
        for phrase in _phrases(script):
            if not _pairs(phrase.split()) & given:
                self._counts[phrase] += 1
                self._last_seen[phrase] = self._scripts

    def worn(self) -> list[str]:
        """여러 멘트에 나온 표현 — 많이 쓴 것, 최근에 쓴 것, 긴 것 순. 이미 고른 표현과 겹치는 묶음은 뺀다."""
        found = [p for p, n in self._counts.items() if n >= self.min_scripts]
        found.sort(key=lambda p: (-self._counts[p], -self._last_seen[p], -len(p), p))
        picked: list[str] = []
        seen: set[tuple[str, str]] = set()
        for phrase in found:
            pairs = _pairs(phrase.split())
            if pairs & seen:  # 같은 구절에서 나온 묶음이다 — 하나만 알린다
                continue
            picked.append(phrase)
            seen |= pairs
            if len(picked) == self.max_phrases:
                break
        return picked
