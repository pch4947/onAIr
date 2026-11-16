"""신청곡 판정 — 신청한 곡이 방의 장르와 분위기에 어울리는지 본다 (계약 6.4절).

방 생성에서 고른 장르가 DJ 선곡에는 쓰이지만(#295) 청취자의 신청곡에는 쓰이지 않았다. 발라드 방에 어떤 곡을
신청해도 그대로 대기열에 들어갔다. 백엔드가 음원을 받기 전에 엔진에 묻고, 어울리지 않으면 신청 화면에서
바로 알려 준다.

**확실히 어울리지 않을 때만 거절한다.** 맞는 곡을 거절당하는 쪽이 청취자에게 더 나쁜 경험이다 —
LLM이 모르는 곡이거나 판단이 서지 않으면 통과시키고, 제공자 장애나 해석 실패도 통과로 답한다.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from ..domain import Prompt, StationProfile
from ..pipeline.llm import LlmError
from ..prompt_data import DATA_RULES, GENRES, STATION_CONCEPT, TOPIC, TRACK, data_block

log = logging.getLogger(__name__)

MARKER = "신청곡이 방송에 어울리는지 판정"  # 더미 LLM이 이 요청을 알아보는 구절 — 지우거나 다른 지시에 쓰지 않는다
MAX_REASON_CHARS = 80
MAX_CACHE = 200

_SYSTEM = ("너는 라디오 방송의 선곡 담당이다. 청취자가 신청한 곡이 이 방송에 틀 만한지 판정한다.\n"
           + DATA_RULES)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class Fit:
    fit: bool
    judged: bool  # 판정을 실제로 했다 — False면 판정하지 못해 통과시킨 것이다
    reason: str = ""  # 거절한 까닭 한 줄 — 엔진 패널과 기록용

    def payload(self) -> dict:
        return {"fit": self.fit, "judged": self.judged, "reason": self.reason or None}


PASS_UNJUDGED = Fit(True, False)


def build_prompt(profile: StationProfile, genres: list[str], topic: str | None, title: str, artist: str) -> Prompt:
    lines = [
        f"{MARKER}: 아래 신청곡을 이 방송에서 틀어도 되는지 판정한다.",
        ("- 기준은 아래 선곡 취향의 장르와 스테이션 컨셉이다." if genres
         else "- 기준은 아래 스테이션 컨셉이다."),
        ("- **확실히 어울리지 않을 때만** 어울리지 않는다고 한다. 장르가 조금 다르거나 이웃한 장르이거나, "
         "분위기가 방송과 통하면 어울린다고 한다."),
        ("- 예를 들어 잔잔한 발라드 방송에 시끄러운 메탈·하드코어 EDM·행사용 트로트·동요·광고 음악은 어울리지 않는다. "
         "같은 발라드 방송에 어쿠스틱·인디·알앤비·잔잔한 팝은 어울린다."),
        "- 모르는 곡이거나 어떤 곡인지 확신할 수 없으면 known을 false로 하고 어울린다고 한다. 추측으로 거절하지 않는다.",
        ("- 곡 정보는 영상 제목이라 'Official MV', '가사', 'Live' 같은 말과 채널 이름이 섞여 있을 수 있다. "
         "그런 말은 무시하고 곡 자체를 본다."),
        "- 오늘의 화제와 어울리는지는 보지 않는다. 화제와 상관없는 곡을 신청해도 된다.",
        ('- 출력은 JSON 하나만: {"fit": true, "known": true, "reason": "어울리지 않을 때만 까닭 한 문장"}'),
        "",
        data_block(STATION_CONCEPT, profile.concept),
    ]
    if genres:
        lines.append(data_block(GENRES, ", ".join(genres)))
    if topic:
        lines.append(data_block(TOPIC, topic))
    lines.append(data_block(TRACK, f"곡: {title}" + (f" — {artist}" if artist else "")))
    return Prompt(system=_SYSTEM, user="\n".join(lines))


def parse_fit(text: str) -> Fit:
    """LLM 출력의 판정. 형식이 틀리면 ValueError."""
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError("JSON이 없습니다")
    data = json.loads(match.group(0))
    if not isinstance(data, dict) or not isinstance(data.get("fit"), bool):
        raise ValueError("fit이 없습니다")  # noqa: TRY004 — 호출자는 ValueError 하나로 받는다
    known = data.get("known")
    if data["fit"] or known is False:  # 모르는 곡은 거절하지 않는다
        return Fit(True, True)
    reason = data["reason"].strip()[:MAX_REASON_CHARS] if isinstance(data.get("reason"), str) else ""
    return Fit(False, True, reason)


def dummy_fit_json(prompt: Prompt) -> str:
    """더미 LLM 응답 — 늘 어울린다. API 키 없이도 신청곡이 막히지 않는다."""
    return json.dumps({"fit": True, "known": True, "reason": ""}, ensure_ascii=False)


class SongFitJudge:
    """방송 하나의 신청곡 판정. 같은 곡을 다시 물으면 같은 답을 준다."""

    def __init__(self, llm, profile: StationProfile, genres: list[str], topic: str | None, *,
                 violates, telemetry=None, max_tokens: int = 600):
        self._llm = llm
        self.profile = profile
        self.genres = list(genres)
        self.topic = topic
        self._violates = violates
        self._telemetry = telemetry
        self.max_tokens = max_tokens
        self._cache: dict[tuple[str, str], Fit] = {}

    def use_persona(self, profile: StationProfile, genres: list[str]) -> None:
        """방송 중에 DJ가 바뀌었다 (#261) — 새 DJ의 컨셉과 선곡 취향으로 판정한다."""
        self.profile = profile
        self.genres = list(genres)
        self._cache.clear()

    async def judge(self, title: str, artist: str = "") -> Fit:
        key = (title.strip().lower(), artist.strip().lower())
        if key in self._cache:
            return self._cache[key]
        # 곡 정보는 외부 텍스트다 — 방송에 못 나갈 말이 든 제목은 판정하지 않고 통과시킨다(거르는 것은 곡 소개 때의 몫)
        if self._violates(f"{title} {artist}") is not None:
            return PASS_UNJUDGED
        try:
            text = await self._llm.complete(
                build_prompt(self.profile, self.genres, self.topic, title.strip(), artist.strip()),
                max_tokens=self.max_tokens)
            result = parse_fit(text)
        except (LlmError, ValueError, OSError) as exc:  # 제공자 장애·해석 실패 — 신청을 막지 않는다
            log.warning("신청곡 판정 실패, 통과시킨다: %s", exc)
            return PASS_UNJUDGED
        if len(self._cache) >= MAX_CACHE:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = result
        if self._telemetry is not None:
            self._telemetry.log_song_fit(title.strip(), artist.strip(), result)
        return result
