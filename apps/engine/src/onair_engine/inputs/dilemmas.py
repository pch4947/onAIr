"""DJ 대 청취자 코너의 고민 — 오늘 주제에서 양자택일 고민을 미리 만든다 (CORNER_DESIGN 7.6절, #279).

게시판의 고민 사연을 구분하는 방법(U6)이 정해지기 전까지는 엔진이 주제에서 만든다.
DJ의 선택도 여기서 함께 정한다 — 투표 결과를 보기 전에 정해 두어야 겨루기가 공정하고, 같은 방송을 다시 볼 때도 같다.
방송 시작과 함께 백그라운드로 만들고, 실패하면 고민이 없어 코너가 주제 토크로 대체된다.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from ..domain import Prompt, StationProfile
from ..pipeline.llm import LlmError
from ..prompt_data import DATA_RULES, TOPIC, data_block

log = logging.getLogger(__name__)

MARKER = "양자택일 고민 만들기"  # 더미 LLM이 이 요청을 알아보는 구절 — 지우거나 다른 지시에 쓰지 않는다
MAX_QUESTION_CHARS = 80
MAX_OPTION_CHARS = 12  # 투표 화면의 버튼과 DJ의 말에 그대로 쓰인다
MAX_REASON_CHARS = 100

_SYSTEM = ("너는 라디오 방송 구성 작가다. 청취자 투표로 겨루는 코너에 쓸 양자택일 고민을 만든다.\n"
           + DATA_RULES)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_COUNT_RE = re.compile(r"고민 (\d+)개")


@dataclass(frozen=True)
class Dilemma:
    question: str
    options: tuple[str, str]
    pick: int  # DJ의 선택 — options의 번호 (0 또는 1)
    reason: str  # DJ가 그렇게 고른 까닭 한 줄. 결과를 공개할 때 쓴다

    def text(self) -> str:
        return f"{self.question}\n선택지: {self.options[0]} / {self.options[1]}\n{self.reason}"


def build_prompt(topic: str, count: int, profile: StationProfile) -> Prompt:
    lines = [
        f"{MARKER}: 아래 주제로 청취자가 둘 중 하나를 고를 고민 {count}개를 만든다.",
        "- 가볍게 웃으며 고를 수 있는 취향·습관의 문제로 한다. 옳고 그름이 갈리는 문제, 건강·돈·연애 상담은 넣지 않는다.",
        f"- 고민은 한 문장으로 쓴다. 선택지는 둘이고, 각각 {MAX_OPTION_CHARS}자 이내의 짧은 말로 쓴다.",
        "- 고민끼리 겹치지 않게 한다.",
        (f"- 고민마다 DJ {profile.dj_name}의 선택(pick: 0이면 첫째, 1이면 둘째)과 그렇게 고른 까닭 한 문장을 붙인다. "
         f"DJ의 말투는 {profile.tone}."),
        "- 까닭은 취향을 말한다. DJ가 직접 겪은 일처럼 쓰지 않고, 확인할 수 없는 사실은 넣지 않는다.",
        ('- 출력은 JSON 하나만: {"dilemmas": [{"question": "고민", "options": ["선택지", "선택지"], '
         '"pick": 0, "reason": "까닭"}]}'),
        "",
        data_block(TOPIC, topic),
    ]
    return Prompt(system=_SYSTEM, user="\n".join(lines))


def parse_dilemmas(text: str) -> list[Dilemma]:
    """LLM 출력의 고민. 형식이 틀린 고민은 버리고, 하나도 없으면 ValueError."""
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError("JSON이 없습니다")
    data = json.loads(match.group(0))
    raw = data.get("dilemmas") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError("dilemmas 배열이 없습니다")  # noqa: TRY004 — 호출자는 ValueError 하나로 받는다
    out = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("question"), str):
            continue
        options = item.get("options")
        if (not isinstance(options, list) or len(options) != 2
                or not all(isinstance(o, str) and o.strip() for o in options)):
            continue
        options = tuple(o.strip()[:MAX_OPTION_CHARS] for o in options)
        pick = item.get("pick")
        if isinstance(pick, str) and pick.strip() in ("0", "1"):  # 번호를 문자열로 주기도 한다 (#277)
            pick = int(pick.strip())
        if isinstance(pick, bool) or pick not in (0, 1) or options[0] == options[1]:
            continue
        question = item["question"].strip()[:MAX_QUESTION_CHARS]
        reason = item["reason"].strip()[:MAX_REASON_CHARS] if isinstance(item.get("reason"), str) else ""
        if question:
            out.append(Dilemma(question, options, pick, reason))
    if not out:
        raise ValueError("쓸 수 있는 고민이 없습니다")
    return out


def dummy_dilemmas_json(prompt: Prompt) -> str:
    """더미 LLM 응답 — 고정 고민. API 키 없이도 코너가 열리게 한다."""
    match = _COUNT_RE.search(prompt.user)
    count = int(match.group(1)) if match else 3
    fixed = [
        {"question": "붕어빵은 머리부터 먹을까요, 꼬리부터 먹을까요?", "options": ["머리부터", "꼬리부터"],
         "pick": 1, "reason": "바삭한 쪽을 먼저 먹고 싶어서요."},
        {"question": "추운 날 간식은 따뜻한 게 좋을까요, 차가운 게 좋을까요?", "options": ["따뜻한 것", "차가운 것"],
         "pick": 0, "reason": "손부터 녹이고 싶어서요."},
        {"question": "간식은 혼자 먹는 게 좋을까요, 나눠 먹는 게 좋을까요?", "options": ["혼자", "나눠서"],
         "pick": 1, "reason": "같이 먹으면 이야기가 생기니까요."},
        {"question": "달콤한 간식과 짭짤한 간식 중 하나만 고른다면요?", "options": ["달콤한 것", "짭짤한 것"],
         "pick": 0, "reason": "하루 끝에는 단 게 당겨서요."},
        {"question": "간식은 사자마자 먹을까요, 집에 가서 먹을까요?", "options": ["사자마자", "집에 가서"],
         "pick": 0, "reason": "따뜻할 때가 제일 맛있어서요."},
    ]
    return json.dumps({"dilemmas": fixed[:count]}, ensure_ascii=False)


class DilemmaPlan:
    """방송 하나의 고민 목록. 준비되면 on_ready로 넘긴다 — 코너의 진입 조건(requires: vote)이 그때 채워진다."""

    def __init__(self, llm, topic: str, profile: StationProfile, *, count: int, violates, on_ready,
                 telemetry=None, max_tokens: int = 4000):
        self._llm = llm
        self._topic = topic
        self._profile = profile
        self._count = count
        self._violates = violates  # 방송에 나갈 말의 재료다 — 대본과 같은 L2 규칙을 통과한 것만 남긴다
        self._on_ready = on_ready
        self._telemetry = telemetry
        self.max_tokens = max_tokens

    async def run(self) -> None:
        try:
            text = await self._llm.complete(build_prompt(self._topic, self._count, self._profile),
                                            max_tokens=self.max_tokens)
            dilemmas = parse_dilemmas(text)
        except (LlmError, ValueError, OSError) as exc:  # 제공자 장애·해석 실패 — 코너가 주제 토크로 대체된다
            log.warning("고민 만들기 실패, DJ 대 청취자 코너는 대체된다: %s", exc)
            dilemmas, method = [], "failed"
        else:
            method = "llm"
        kept = [d for d in dilemmas if self._violates(d.text()) is None]
        if self._telemetry is not None:
            self._telemetry.log_dilemmas(len(kept), method, dropped=len(dilemmas) - len(kept))
        self._on_ready(kept)
