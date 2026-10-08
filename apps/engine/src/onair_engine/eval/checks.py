"""대본 자동 지표 — LLM 없이 계산되는 규칙 기반 검사 (docs/persona.md 7절 품질 기준).

여기 지표는 "의심 신호"다. 말투가 맞는지, 라디오답게 들리는지의 최종 판단은 사람이 읽거나
LLM-judge(runner.py --judge)로 한다. 모든 함수는 순수 함수라 단위 테스트로 고정한다.
"""
from __future__ import annotations

import re

# [예시] 한국어 TTS 발화 속도. 실제 오디오 길이(duration_ms)로 보정한다
SYLLABLES_PER_SEC = 6.0

_SENTENCE_RE = re.compile(r"[^.!?…~]+[.!?…~]*")
_TRAIL = " .!?…~,"
# 존댓말 종결: 해요체·하십시오체. "~죠"는 "~지요"의 준말이다
_POLITE_END = re.compile(r"(요|니다|니까|죠)$")
# 반말·해라체 종결 — "~다"는 "입니다"가 위에서 먼저 걸리므로 여기서는 해라체만 남는다
_CASUAL_END = re.compile(
    r"(다|야|어|아|지|네|자|래|걸|게|까|니|냐|군|구나|해|거든|잖아|더라|든|라)$")
_MARKUP_RE = re.compile(r"[*#_`<>\[\](){}]|[\U0001F300-\U0001FAFF☀-➿]")
_LATIN_RE = re.compile(r"[A-Za-z]{2,}")
_DIGIT_RE = re.compile(r"\d")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.findall(text) if s.strip(_TRAIL)]


def est_seconds(text: str) -> float:
    """발화 시간 추정 — 공백·문장부호를 뺀 글자 수 / 발화 속도."""
    units = sum(1 for ch in text if ch.isalnum())
    return round(units / SYLLABLES_PER_SEC, 1)


def speech_level(sentence: str) -> str | None:
    """'polite' | 'casual' | None(명사로 끝나는 등 판단 불가)."""
    s = sentence.rstrip(_TRAIL)
    if _POLITE_END.search(s):
        return "polite"
    if _CASUAL_END.search(s):
        return "casual"
    return None


def expected_level(tone: str) -> str | None:
    """persona tone 문자열에서 기대 말투를 읽는다. 둘 다 없으면 검사하지 않는다."""
    # "반말. 존댓말을 섞지 않는다"처럼 반말 설정이 존댓말을 부정형으로 언급하는 경우가 많다
    if "반말" in tone:
        return "casual"
    if "존댓말" in tone:
        return "polite"
    return None


def level_violations(text: str, expected: str | None, source: str = "") -> list[str]:
    """기대 말투와 반대로 끝난 문장들.

    source(프롬프트 소재)에 있는 단어로 끝나는 문장은 뺀다 — "아이유, 밤편지."의 '지'는 어미가 아니다.
    """
    if expected is None:
        return []
    return [s for s in sentences(text)
            if speech_level(s) not in (None, expected)
            and s.rstrip(_TRAIL).split()[-1] not in source]


def leaked_markup(text: str) -> list[str]:
    """TTS가 읽으면 사고가 나는 기호 — clean_script를 빠져나온 것."""
    return _MARKUP_RE.findall(text)


def latin_words(text: str) -> list[str]:
    """영어 단어 — 곡명·아티스트명은 정상일 수 있어 위반이 아니라 확인 대상이다."""
    return _LATIN_RE.findall(text)


def has_digits(text: str) -> bool:
    """숫자 표기 — '1시'를 '일 시'로 읽는 등 TTS 발음이 어긋날 수 있다."""
    return bool(_DIGIT_RE.search(text))


def found(text: str, phrases: list[str]) -> list[str]:
    compact = text.replace(" ", "")
    return [p for p in phrases if p.replace(" ", "") in compact]


def opener(text: str) -> str:
    """첫마디 — 매 멘트가 같은 인사로 시작하는지 본다."""
    return " ".join(text.split()[:2]).rstrip(_TRAIL)


def _ngrams(text: str, n: int = 3) -> set[str]:
    compact = "".join(ch for ch in text if ch.isalnum())
    return {compact[i:i + n] for i in range(len(compact) - n + 1)}


def similarity(a: str, b: str) -> float:
    """글자 3-gram 자카드 유사도 0~1."""
    ga, gb = _ngrams(a), _ngrams(b)
    if not ga or not gb:
        return 0.0
    return round(len(ga & gb) / len(ga | gb), 3)


def max_similarity(text: str, previous: list[str]) -> float:
    return max((similarity(text, p) for p in previous), default=0.0)


def sentence_limit(instructions: str) -> int | None:
    """코너 지시문의 'N문장 이내' / '1~2문장' 상한."""
    nums = [int(n) for n in re.findall(r"(\d+)문장", instructions)]
    return max(nums) if nums else None
