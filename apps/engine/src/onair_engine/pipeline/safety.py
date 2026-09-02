"""안전 계층 L0/L2 — 룰은 config/safety_rules.yaml에서 로드 (설계 문서 4.6).

L1(생성단)은 별도 모듈이 아니라 코너의 프롬프트에 내장된다 (corners/base.py).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


class SafetyChecker:
    def __init__(self, rules_path: str | Path):
        raw = yaml.safe_load(Path(rules_path).read_text(encoding="utf-8")) or {}
        self._patterns = [re.compile(p) for p in raw.get("banned_patterns", [])]

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
