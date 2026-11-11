"""이야기 갈래 계획 — 주제 토크가 들어올 때마다 같은 말을 되풀이하지 않게 할 말할 거리 (CORNER_DESIGN 5.1.1절).

주제 한 줄만 받은 LLM은 매번 "겨울 간식은 …"으로 시작해 "어떤 간식이 떠오르세요?"로 끝난다 (#222 지표).
그래서 방송 시작 때 LLM에 한 번 물어 오늘 주제를 서로 겹치지 않는 갈래 여러 개로 펼쳐 둔다.
실패하면 갈래 없이 주제 한 줄로 말한다 — 방송은 멈추지 않는다.

갈래 하나를 한 번에 다 쓰지 않는다. 주제 토크는 들어올 때마다 말할 거리를 몇 개씩 꺼내고, 남았으면 다음에
들어와서 같은 갈래를 잇는다. 들어올 때마다 갈래를 바꾸면 곡이 끝날 때마다 소주제가 바뀌어 토막 난다 (#262).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from ..domain import Prompt
from ..pipeline.llm import LlmError
from ..prompt_data import DATA_RULES, TOPIC, data_block

log = logging.getLogger(__name__)

MARKER = "이야기 갈래 계획"  # 더미 LLM이 이 요청을 알아보는 구절 — 지우거나 다른 지시에 쓰지 않는다
SEC_PER_ANGLE = 600  # [예시] 주제 토크는 대략 5분에 한 번 돌아오고, 갈래 하나를 두 번에 걸쳐 쓴다 (talk.yaml cycle)
MIN_ANGLES, MAX_ANGLES = 2, 12
MAX_POINTS = 6
MAX_TITLE_CHARS = 40
MAX_POINT_CHARS = 100

_SYSTEM = ("너는 라디오 방송 구성 작가다. 오늘 주제로 DJ가 방송 중에 꺼낼 이야기 갈래를 짠다.\n"
           + DATA_RULES)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_COUNT_RE = re.compile(r"갈래 (\d+)개")


@dataclass(frozen=True)
class Angle:
    title: str
    points: tuple[str, ...]

    def text(self) -> str:
        return "\n".join([self.title, *(f"- {p}" for p in self.points)])


def angle_count(broadcast_minutes: int) -> int:
    return max(MIN_ANGLES, min(MAX_ANGLES, round(broadcast_minutes * 60 / SEC_PER_ANGLE)))


def build_prompt(topic: str, count: int, forbidden: list[str] | None = None) -> Prompt:
    lines = [
        f"{MARKER}: 아래 주제를 서로 겹치지 않는 이야기 갈래 {count}개로 펼친다.",
        "- 갈래마다 짧은 제목 하나와 DJ가 말할 거리 4~6개를 붙인다.",
        "- 말할 거리는 순서대로 이어지게 쓴다. 앞의 것으로 이야기를 꺼내고, 가운데에서 풀어 가고, 마지막 것으로 마무리한다.",
        "- 말할 거리는 떠오르는 장면, 흔히 겪는 일, 둘 중 하나 고르기처럼 구체적으로 쓴다. 질문 문장으로 쓰지 않는다.",
        "- 수치, 가격, 상호, 날짜, 역사적 사실처럼 확인할 수 없는 사실은 넣지 않는다.",
        "- DJ가 직접 겪은 일처럼 쓰지 않는다.",
    ]
    if forbidden:  # persona의 금지 항목 — 대본 생성과 같은 기준을 갈래에도 건다
        lines.append("- 다음은 넣지 않는다: " + "; ".join(forbidden))
    lines += [
        '- 출력은 JSON 하나만: {"angles": [{"title": "갈래 제목", "points": ["말할 거리", ...]}]}',
        "",
        data_block(TOPIC, topic),
    ]
    return Prompt(system=_SYSTEM, user="\n".join(lines))


def parse_angles(text: str) -> list[Angle]:
    """LLM 출력의 갈래. 형식이 틀린 갈래는 버리고, 하나도 없으면 ValueError."""
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError("JSON이 없습니다")
    data = json.loads(match.group(0))
    raw = data.get("angles") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError("angles 배열이 없습니다")  # noqa: TRY004 — 호출자는 ValueError 하나로 받는다
    angles = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str):
            continue
        points = item.get("points") if isinstance(item.get("points"), list) else []
        points = tuple(p.strip()[:MAX_POINT_CHARS] for p in points if isinstance(p, str) and p.strip())
        if title := item["title"].strip()[:MAX_TITLE_CHARS]:
            angles.append(Angle(title, points[:MAX_POINTS]))
    if not angles:
        raise ValueError("쓸 수 있는 갈래가 없습니다")
    return angles[:MAX_ANGLES]


def dummy_plan_json(prompt: Prompt) -> str:
    """더미 LLM 응답 — 주제 이름을 붙인 갈래. API 키 없이도 이어 말하기 흐름을 돌려 볼 수 있게 한다."""
    m = _COUNT_RE.search(prompt.user)
    count = int(m.group(1)) if m else MIN_ANGLES
    return json.dumps({"angles": [
        {"title": f"{n}번째 이야기", "points": ["떠오르는 장면 하나", "흔히 겪는 일 하나", "둘 중 하나 고르기",
                                              "마무리하는 생각 하나"]}
        for n in range(1, count + 1)
    ]}, ensure_ascii=False)


NEW, CONTINUE = "new", "continue"


class TopicPlan:
    """방송 하나의 갈래 목록. 주제 토크가 들어올 때마다 take()로 말할 거리를 몇 개씩 꺼낸다."""

    def __init__(self, llm, topic: str, *, count: int, violates, forbidden: list[str] | None = None,
                 telemetry=None, max_tokens: int = 4000):
        self._llm = llm
        self._topic = topic
        self._count = count
        self._violates = violates  # 방송에 나갈 말의 재료다 — 대본과 같은 L2 규칙을 통과한 것만 남긴다
        self._forbidden = forbidden
        self._telemetry = telemetry
        self.max_tokens = max_tokens
        self._angles: list[Angle] = []
        self.used: list[Angle] = []  # 꺼낸 순서대로 — 다음 갈래 프롬프트에 "이미 다룬 갈래"로 넣는다
        self._points_used = 0  # 지금 갈래(used의 마지막)에서 꺼낸 말할 거리 수
        self.ready = False

    async def run(self) -> None:
        """갈래를 만든다. 방송 시작과 함께 백그라운드로 돈다 — 첫 주제 토크 전에 끝나지 않으면 그 진입은 갈래 없이 간다."""
        try:
            text = await self._llm.complete(build_prompt(self._topic, self._count, self._forbidden),
                                            max_tokens=self.max_tokens)
            angles = parse_angles(text)
        except (LlmError, ValueError, OSError) as exc:  # 제공자 장애·해석 실패 — 주제 한 줄로 진행한다
            log.warning("이야기 갈래 계획 실패, 주제만으로 진행한다: %s", exc)
            angles, method = [], "failed"
        else:
            method = "llm"
        kept = [a for a in angles if self._violates(a.text()) is None]
        self._angles = kept
        self.ready = True
        if self._telemetry is not None:
            self._telemetry.log_topic_plan(kept, method, dropped=len(angles) - len(kept))

    @property
    def current(self) -> Angle | None:
        return self.used[-1] if self.used else None

    def take(self, n: int) -> tuple[Angle | None, str | None]:
        """이번 진입에 쓸 말할 거리 n개까지 — (갈래 제목과 그 말할 거리, NEW | CONTINUE).

        하던 갈래에 말할 거리가 남았으면 그것을 잇고, 다 썼으면 다음 갈래로 넘어간다.
        아직 준비되지 않았거나 갈래를 다 썼으면 (None, None).
        """
        state = CONTINUE
        if (points := self._remaining(n)) is None:
            if not self._angles:
                return None, None
            angle = self._angles.pop(0)
            self.used.append(angle)
            self._points_used = 0
            if self._telemetry is not None:
                self._telemetry.log_angle(angle, len(self.used), len(self.used) + len(self._angles))
            state, points = NEW, self._remaining(n) or ()
        return Angle(self.current.title, points), state

    def more(self, n: int = 1) -> tuple[str, ...]:
        """하던 갈래의 다음 말할 거리 — 갈래를 넘기지 않는다. 다 썼으면 빈 튜플."""
        return self._remaining(n) or ()

    def _remaining(self, n: int) -> tuple[str, ...] | None:
        angle = self.current
        if angle is None or self._points_used >= len(angle.points):
            return None
        points = angle.points[self._points_used:self._points_used + n]
        self._points_used += len(points)
        return points

    def next_angle(self) -> Angle | None:
        """다음 갈래를 통째로 — 하던 갈래에 남은 말할 거리는 버린다. 다 썼으면 None."""
        if not self._angles:
            return None
        self._points_used = len(self.current.points) if self.current else 0
        angle, _ = self.take(MAX_POINTS)
        return angle
