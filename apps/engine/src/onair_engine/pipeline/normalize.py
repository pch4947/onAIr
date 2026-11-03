"""TTS 직전 텍스트 정규화 — 채팅 표기를 소리로 읽을 수 있게 바꾼다 (CORNER_DESIGN U15).

Gemini TTS에는 발음 사전이 없어 엔진이 바꾼다. audio.CachedTts가 모든 합성 앞에서 부른다.
"""
from __future__ import annotations

import re

_RUNS = (
    (re.compile(r"(?:ㅊㅋ)+"), lambda m: "축하" * (len(m.group()) // 2)),
    (re.compile(r"ㅇ{2,}"), lambda _: "응응"),
    (re.compile(r"ㄷ{2,}"), lambda _: "덜덜"),
    (re.compile(r"ㄱ{2,}"), lambda _: "고고"),
    (re.compile(r"ㅋ+"), lambda m: "크" * min(len(m.group()), 3)),
    (re.compile(r"ㅠ+"), lambda m: "유" * min(len(m.group()), 3)),
    (re.compile(r"ㅜ+"), lambda m: "우" * min(len(m.group()), 3)),
    (re.compile(r"ㅎ+"), lambda _: "흐흐"),
)
_LONE_JAMO = re.compile(r"[ㄱ-ㅣ]")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_SPACES = re.compile(r"[ \t]{2,}")


def normalize_for_tts(text: str) -> str:
    """바꾼 결과가 비면 원문을 돌려준다 — 합성할 문장이 사라지면 안 된다."""
    out = text
    for pattern, replace in _RUNS:
        out = pattern.sub(replace, out)
    out = _EMOJI.sub("", _LONE_JAMO.sub("", out))
    out = _SPACES.sub(" ", out).strip()
    return out or text
