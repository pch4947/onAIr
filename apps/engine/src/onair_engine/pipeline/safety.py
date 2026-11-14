"""안전 계층 L0/L2 — 룰은 config/safety_rules.yaml에서 로드 (설계 문서 4.6).

L1(생성단)은 별도 모듈이 아니라 코너의 프롬프트에 내장된다 (corners/base.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_SENTENCE_END = re.compile(r"(?<=[.?!…])\s+")
# 작품·프로그램 이름과 인용문 — 주관 표현 검사에서 뺀다 ('놀라운 토요일', "안타깝다")
_QUOTED = re.compile(r"‘[^’]*’|“[^”]*”|'[^']*'|\"[^\"]*\"")
# 남의 말을 옮긴 문장이어도 보는 묶음 — DJ가 한 말일 수밖에 없다
_ALWAYS = ("first_person", "address", "reaction_ending")
SIGNATURE = "signature"  # 입버릇 — 뉴스에서는 끈다 (6.4절 3번)
FORECAST = "forecast"  # 출처 없는 전망


@dataclass(frozen=True)
class Subjective:
    """뉴스 대본에서 걸린 문장 하나."""

    sentence: str
    rule: str  # 묶음 이름 (evaluation, watch, wish, judgment, guess, first_person, address, reaction_ending, …)
    match: str  # 걸린 표현


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text.strip()) if s.strip()]


class SafetyChecker:
    def __init__(self, rules_path: str | Path):
        raw = yaml.safe_load(Path(rules_path).read_text(encoding="utf-8")) or {}
        self._patterns = [re.compile(p) for p in raw.get("banned_patterns", [])]
        # 뉴스 대본의 주관 표현 (CORNER_DESIGN 6.4절) — 묶음 이름과 함께 들고 있다가 걸린 묶음을 기록한다
        self._subjective = [(name, re.compile(p)) for name, patterns in (raw.get("news_subjective") or {}).items()
                            for p in patterns]
        self._forecast = [re.compile(p) for p in raw.get("news_forecast", [])]
        self._attribution = [re.compile(p) for p in raw.get("news_attribution", [])]
        self._reported = [re.compile(p) for p in raw.get("news_reported", [])]

    def _check(self, text: str) -> str | None:
        """위반한 패턴을 반환. 통과 시 None."""
        for pat in self._patterns:
            if pat.search(text):
                return pat.pattern
        return None

    # L0(입력단)과 L2(출력단)는 현재 동일 룰셋 — 계측 구분을 위해 이름을 나눠 둔다
    def check_l0(self, text: str) -> str | None:
        return self._check(text)

    def check_l2(self, text: str) -> str | None:
        return self._check(text)

    def check_news(self, text: str, signature_phrases: list[str] | tuple[str, ...] = ()) -> list[Subjective]:
        """뉴스 대본에서 DJ의 의견·평가·전망이 섞인 문장 (CORNER_DESIGN 6.4절). 없으면 빈 목록.

        문장마다 본다. 남의 말을 옮긴 것("안타깝다고 말했습니다")과 따옴표 안은 걸지 않고,
        전망은 같은 문장에 출처가 있으면 전하는 말로 본다. 입버릇도 뉴스에서는 쓰지 않는다.
        """
        return [hit for sentence in split_sentences(text)
                if (hit := self._subjective_in(sentence, signature_phrases)) is not None]

    def _subjective_in(self, sentence: str, signature_phrases) -> Subjective | None:
        bare = _QUOTED.sub("", sentence)
        for phrase in signature_phrases:
            if phrase and phrase.strip(" .!?~") in bare:
                return Subjective(sentence, SIGNATURE, phrase)
        for name, pattern in self._subjective:
            for m in pattern.finditer(bare):
                if name not in _ALWAYS and any(r.search(bare, m.end()) for r in self._reported):
                    continue  # 남의 말을 옮겼다
                return Subjective(sentence, name, m.group(0))
        for pattern in self._forecast:
            if (m := pattern.search(bare)) and not any(a.search(bare) for a in self._attribution):
                return Subjective(sentence, FORECAST, m.group(0))
        return None
