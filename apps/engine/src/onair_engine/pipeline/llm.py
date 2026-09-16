"""LLM 어댑터 — 제공자 미정(확인 3)이므로 더미가 기본. M1에서 실 어댑터 추가."""
from __future__ import annotations

import asyncio
import random
import re
from typing import Protocol

from ..domain import Prompt, Script


class LlmClient(Protocol):
    async def generate(self, prompt: Prompt) -> Script: ...


# 더미 대본 — TTS로 실제로 읽혀도 방송처럼 들리도록 쓴 코너별 고정 문장.
# 슬롯({dj}, {title} 등)은 프롬프트에서 추출해 채운다 (_slots 참고).
# 슬롯 값의 받침 유무를 모르므로 슬롯 바로 뒤에는 조사(은/는, 이/가, 예요/이에요)를 붙이지 않는다.
_TEMPLATES: dict[str, list[str]] = {
    "opening": [
        ("안녕하세요, {concept}의 {dj}입니다. 오늘도 이 시간을 함께해 주셔서 고마워요. "
         "편하게 들으면서 하던 일 이어가세요."),
        "{concept}, 지금 시작합니다. DJ {dj}입니다. 잠깐 숨 한 번 고르고, 천천히 같이 가봐요.",
        "반가워요, {dj}입니다. 오늘 하루는 어떠셨나요? 지금부터 조용히 곁에 있을게요.",
    ],
    "briefing": [
        "잠깐 소식 하나 전해드릴게요. {headline}. 자세한 내용은 원문 출처에서 확인해 보세요.",
        "요즘 이런 이야기가 있더라고요. {headline}. 다음 곡 듣기 전에 한 번쯤 생각해 볼 만하죠.",
    ],
    "music_intro": [
        "이번 곡은 {artist}의 {title}입니다. {mood} 분위기라 지금 시간에 잘 어울릴 거예요.",
        "잠깐 쉬어가는 의미로 한 곡 준비했어요. {artist}, {title}. 이어서 들려드릴게요.",
        "{mood} 곡 하나 골라봤어요. {artist}의 {title}, 함께 들어요.",
    ],
    "request_reply": [
        ("사연 하나 읽어드릴게요. {story}. 이렇게 보내주셨어요. 그런 날도 있죠. "
         "이 시간만큼은 너무 애쓰지 말고 편하게 쉬어가셨으면 좋겠어요."),
        ("보내주신 이야기 잘 받았어요. {story}. 마음이 전해져요. "
         "지금 이 방송을 듣는 모두가 함께라는 걸 기억해 주세요."),
    ],
    "filler": [
        "지금 이 순간도 충분히 잘 해내고 계신 거예요. 곧 다음 곡 이어갈게요.",
        "잠깐 기지개 한 번 켜볼까요? 어깨 힘 빼고, 음악은 계속됩니다.",
        "물 한 모금 마시고 오세요. 저 {dj}, 여기 그대로 있을게요.",
    ],
}

# 실 LLM처럼 프롬프트만 받으므로 코너 종류는 코너 프롬프트 문구로 판별한다.
# corners/*.py의 프롬프트 문구를 바꾸면 여기도 맞춰야 한다 (test_smoke가 어긋남을 검출).
_CORNER_MARKERS = [
    ("오프닝", "opening"),
    ("브리핑", "briefing"),
    ("곡을 소개", "music_intro"),
    ("사연:", "request_reply"),
    ("브릿지", "filler"),
]

_PROFILE_RE = re.compile(r"\[(?P<concept>.+?)\]의 DJ (?P<dj>.+?)다\.")
_TRACK_RE = re.compile(r"곡: (?P<title>.+?) — (?P<artist>.+?) \(무드: (?P<mood>.+?)\)")
_STORY_RE = re.compile(r"사연: (?P<story>.+)", re.DOTALL)
_HEADLINE_RE = re.compile(r"^- (?P<headline>.+?): ", re.MULTILINE)

_MOOD_KO = {"calm": "차분한", "mellow": "포근한", "hopeful": "희망찬"}


def detect_corner(user_prompt: str) -> str | None:
    return next((corner for marker, corner in _CORNER_MARKERS if marker in user_prompt), None)


def _slots(prompt: Prompt) -> dict[str, str]:
    slots = {
        "dj": "DJ", "concept": "우리 방송",
        "title": "다음 곡", "artist": "오늘의 아티스트", "mood": "잔잔한",
        "story": "오늘 하루 이야기", "headline": "오늘의 소식",
    }
    for regex, text in [(_PROFILE_RE, prompt.system), (_TRACK_RE, prompt.user),
                        (_STORY_RE, prompt.user), (_HEADLINE_RE, prompt.user)]:
        if m := regex.search(text):
            slots.update({k: v.strip() for k, v in m.groupdict().items()})
    slots["mood"] = _MOOD_KO.get(slots["mood"], slots["mood"])
    slots["story"] = slots["story"][:60]
    return slots


class DummyLlmClient:
    """코너별 고정 대본 반환 — API 키 없이 파이프라인 관통·청취 테스트·발표장 폴백용."""

    def __init__(self, delay_sec: float = 0.2, seed: int | None = None):
        self.delay_sec = delay_sec
        self._rng = random.Random(seed)

    async def generate(self, prompt: Prompt) -> Script:
        await asyncio.sleep(self.delay_sec)  # 생성 지연 흉내
        corner = detect_corner(prompt.user) or "filler"
        template = self._rng.choice(_TEMPLATES[corner])
        return Script(text=template.format_map(_slots(prompt)))


def make_llm(kind: str) -> LlmClient:
    if kind == "dummy":
        return DummyLlmClient()
    raise ValueError(f"unknown llm adapter: {kind}")  # TODO(M1): 실제 제공자 어댑터
