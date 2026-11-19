"""이야기 갈래 계획 — 주제 토크가 들어올 때마다 같은 말을 되풀이하지 않게 할 말할 거리 (CORNER_DESIGN 5.1.1절).

주제 한 줄만 받은 LLM은 매번 "겨울 간식은 …"으로 시작해 "어떤 간식이 떠오르세요?"로 끝난다 (#222 지표).
그래서 방송 시작 때 LLM에 한 번 물어 오늘 주제를 서로 겹치지 않는 갈래 여러 개로 펼쳐 둔다.
실패하면 갈래 없이 주제 한 줄로 말한다 — 방송은 멈추지 않는다.

갈래 하나를 한 번에 다 쓰지 않는다. 주제 토크는 들어올 때마다 말할 거리를 몇 개씩 꺼내고, 남았으면 다음에
들어와서 같은 갈래를 잇는다. 들어올 때마다 갈래를 바꾸면 곡이 끝날 때마다 소주제가 바뀌어 토막 난다 (#262).

갈래를 빈손으로 짜지 않는다 (#332, CORNER_DESIGN 5.9절). 미리 만들어 둔 이야깃거리 데이터셋에서 주제와 가까운
것을 찾아(inputs/talk_bank.py) 참고 자료로 넣고, 다른 방송이 이미 쓴 갈래는 규칙으로 뺀다
(inputs/topic_usage.py). 둘 다 없거나 실패하면 예전처럼 LLM만으로 짠다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass

from ..domain import Prompt
from ..pipeline.embed import EmbedError
from ..pipeline.llm import LlmError
from ..prompt_data import AIRED_ELSEWHERE, COVERED_ANGLES, DATA_RULES, TALK_BANK, TOPIC, data_block

log = logging.getLogger(__name__)

MARKER = "이야기 갈래 계획"  # 더미 LLM이 이 요청을 알아보는 구절 — 지우거나 다른 지시에 쓰지 않는다
SEC_PER_ANGLE = 600  # [예시] 주제 토크는 대략 5분에 한 번 돌아오고, 갈래 하나를 두 번에 걸쳐 쓴다 (talk.yaml cycle)
MIN_ANGLES, MAX_ANGLES = 2, 12
MAX_POINTS = 6
MAX_TITLE_CHARS = 40
MAX_POINT_CHARS = 100
REFERENCES_PER_ANGLE = 2  # 갈래 하나에 참고 이야깃거리 이만큼을 찾아 준다 — 주제에 안 맞는 것을 버릴 여유 [예시]
MAX_REFERENCES = 12
# 이야깃거리 검색을 기다리는 상한. 벡터 캐시가 비어 있으면 수천 건을 임베딩하느라 1분쯤 걸린다 (2026-11-19 실측:
# 1,189건에 24초). 그때는 이번 방송만 검색 없이 가고 캐시는 뒤에서 마저 채운다. 운영(--serve)은 엔진을 띄울 때
# 미리 채운다 (main.py). 손으로 채우기: onair-bank index [예시]
SEARCH_WAIT_SEC = 20.0
# 데이터셋과 사용 기록은 갈래 계획을 돕는 것이다 — 이것들이 실패해도 계획은 예전처럼 LLM만으로 선다.
# 임베딩 장애만이 아니라 sqlite 파일(벡터 캐시, 사용 기록)이 잠기거나 깨진 것도 여기서 받는다:
# 계획은 뒤에서 도는 작업이라, 새어 나간 예외는 아무도 받지 않고 방송 내내 갈래가 없게 된다
_SOURCE_ERRORS = (EmbedError, TimeoutError, sqlite3.Error, OSError)

_SYSTEM = ("너는 라디오 방송 구성 작가다. 오늘 주제로 DJ가 방송 중에 꺼낼 이야기 갈래를 짠다.\n"
           + DATA_RULES)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_COUNT_RE = re.compile(r"갈래 (\d+)개")


@dataclass(frozen=True)
class Angle:
    title: str
    points: tuple[str, ...]
    source: str | None = None  # 참고한 데이터셋 이야깃거리의 id — 새로 짠 갈래는 None

    def text(self) -> str:
        return "\n".join([self.title, *(f"- {p}" for p in self.points)])


def angle_count(broadcast_minutes: int) -> int:
    return max(MIN_ANGLES, min(MAX_ANGLES, round(broadcast_minutes * 60 / SEC_PER_ANGLE)))


def _references_text(references: list) -> str:
    return "\n".join("\n".join([f"{n}. {item.title}", *(f"- {p}" for p in item.points)])
                     for n, item in enumerate(references, 1))


def build_prompt(topic: str, count: int, forbidden: list[str] | None = None, *,
                 references: list | None = None, avoid: list[str] | None = None,
                 taken: list[str] | None = None) -> Prompt:
    """references는 찾아 둔 이야깃거리(BankItem), avoid는 다른 방송에서 다룬 갈래의 제목,
    taken은 이 방송에 이미 짜 둔 갈래의 제목(빠진 만큼 다시 짤 때)."""
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
    blocks = [data_block(TOPIC, topic)]
    if references:
        # 번호로 출처를 받는다 — 긴 id는 LLM이 틀리게 옮긴다
        lines += [
            ("- 아래 참고 이야깃거리는 미리 만들어 둔 자료다. 오늘 주제에 맞는 것을 골라 갈래로 쓴다. "
             "제목과 말할 거리는 주제에 맞게 다듬어도 된다."),
            ("- 참고 이야깃거리로 만든 갈래는 source에 그 번호를 적는다. 주제에 맞는 것이 모자라면 나머지 갈래는 "
             "새로 짜고 source를 null로 둔다. 주제와 동떨어진 자료를 억지로 쓰지 않는다."),
        ]
        blocks.append(data_block(TALK_BANK, _references_text(references)))
        shape = '{"angles": [{"title": "갈래 제목", "points": ["말할 거리", ...], "source": 번호 또는 null}]}'
    else:
        shape = '{"angles": [{"title": "갈래 제목", "points": ["말할 거리", ...]}]}'
    if avoid:
        lines.append("- 아래 다른 방송에서 다룬 갈래와 겹치는 이야기는 짜지 않는다.")
        blocks.append(data_block(AIRED_ELSEWHERE, "\n".join(f"- {t}" for t in avoid)))
    if taken:
        lines.append("- 아래 다룬 갈래는 이 방송에 이미 짜 둔 것이다. 겹치지 않는 이야기로 짠다.")
        blocks.append(data_block(COVERED_ANGLES, "\n".join(f"- {t}" for t in taken)))
    lines +=[f"- 출력은 JSON 하나만: {shape}", "", *blocks]
    return Prompt(system=_SYSTEM, user="\n".join(lines))


def _source(value, sources: list[str]) -> str | None:
    """LLM이 적은 번호 → 이야깃거리 id. 숫자가 아니거나 범위를 벗어나면 새로 짠 갈래로 본다."""
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= len(sources):
        return sources[value - 1]
    return None


def parse_angles(text: str, sources: list[str] | None = None) -> list[Angle]:
    """LLM 출력의 갈래. 형식이 틀린 갈래는 버리고, 하나도 없으면 ValueError.

    sources는 프롬프트에 넣은 참고 이야깃거리의 id — 번호 순서대로.
    """
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
            angles.append(Angle(title, points[:MAX_POINTS], _source(item.get("source"), sources or [])))
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
                 telemetry=None, max_tokens: int = 4000, bank=None, usage=None, station_id: str = "",
                 broadcast_id: str | None = None, fmt: str = "talk", persona: str = ""):
        """bank는 이야깃거리 데이터셋(TalkBank), usage는 방송별 사용 기록(TopicUsage) — 없으면 쓰지 않는다.

        broadcast_id는 이 방송의 id — 사용 기록에서 "다른 방송"을 가린다. 스테이션 id는 고정 채널이라 방송이
        바뀌어도 같으므로 엔진이 방송마다 새로 만들어 준다. 없으면 station_id를 쓴다.
        fmt와 persona(talk_bank.persona_text)는 검색에서 형식과 닮은 프리셋을 고르는 데 쓴다.
        """
        self._llm = llm
        self._topic = topic
        self._count = count
        self._violates = violates  # 방송에 나갈 말의 재료다 — 대본과 같은 L2 규칙을 통과한 것만 남긴다
        self._forbidden = forbidden
        self._telemetry = telemetry
        self.max_tokens = max_tokens
        self._bank = bank
        self._usage = usage
        self._station_id = station_id
        self._broadcast = broadcast_id or station_id
        self._fmt = fmt
        self._persona = persona
        self._vectors: dict = {}  # 갈래 제목 → 벡터 (usage.screen이 만든 것). 꺼낼 때 기록에 함께 남긴다
        self._angles: list[Angle] = []
        self.used: list[Angle] = []  # 꺼낸 순서대로 — 다음 갈래 프롬프트에 "이미 다룬 갈래"로 넣는다
        self._points_used = 0  # 지금 갈래(used의 마지막)에서 꺼낸 말할 거리 수
        self.ready = False

    async def run(self) -> None:
        """갈래를 만든다. 방송 시작과 함께 백그라운드로 돈다 — 첫 주제 토크 전에 끝나지 않으면 그 진입은 갈래 없이 간다."""
        references = await self._references()
        avoid = await self._aired_elsewhere()
        try:
            prompt = build_prompt(self._topic, self._count, self._forbidden, references=references, avoid=avoid)
            text = await self._llm.complete(prompt, max_tokens=self.max_tokens)
            angles = parse_angles(text, [item.id for item in references])
        except (LlmError, ValueError, OSError) as exc:  # 제공자 장애·해석 실패
            if references:  # 찾아 둔 이야깃거리를 다듬지 않고 그대로 쓴다
                log.warning("이야기 갈래 계획 실패, 찾아 둔 이야깃거리를 그대로 쓴다: %s", exc)
                angles = [Angle(item.title, item.points[:MAX_POINTS], item.id) for item in references[:self._count]]
                method = "bank"
            else:
                log.warning("이야기 갈래 계획 실패, 주제만으로 진행한다: %s", exc)
                angles, method = [], "failed"
        else:
            method = "llm"
        kept = [a for a in angles if self._violates(a.text()) is None]
        dropped = len(angles) - len(kept)
        reused: list[Angle] = []
        if self._usage is not None:
            try:
                screened = await self._usage.screen(self._broadcast, kept, keep_min=MIN_ANGLES)
            except _SOURCE_ERRORS as exc:  # 견줄 수 없으면 거르지 않는다
                log.warning("갈래를 다른 방송과 견주지 못했다: %r", exc)
            else:
                kept, reused, self._vectors = screened.kept, screened.reused, screened.vectors
                if reused and method == "llm":
                    more, again = await self._refill(self._count - len(kept), kept,
                                                     [*avoid, *(a.title for a in reused)])
                    kept, reused = kept + more, reused + again
        self._angles = kept
        self.ready = True
        if self._telemetry is not None:
            self._telemetry.log_topic_plan(kept, method, dropped=dropped, reused=[a.title for a in reused])

    async def _refill(self, need: int, kept: list[Angle], avoid: list[str]) -> tuple[list[Angle], list[Angle]]:
        """다른 방송과 겹쳐 빠진 만큼 한 번 더 짠다 — (새 갈래, 이번에도 겹쳐 뺀 갈래).

        채우지 않으면 같은 주제의 방송이 거듭될수록 갈래가 줄어 주제 한 줄로 말하는 시간이 늘어난다
        (2026-11-19 실측: 같은 주제 네 번째 방송에서 6개 중 3개만 남았다, T-017). 한 번만 한다.
        """
        if need <= 0:
            return [], []
        try:
            prompt = build_prompt(self._topic, need, self._forbidden, avoid=list(dict.fromkeys(avoid)),
                                  taken=[a.title for a in kept])
            angles = parse_angles(await self._llm.complete(prompt, max_tokens=self.max_tokens))
        except (LlmError, ValueError, OSError) as exc:
            log.warning("빠진 갈래를 다시 짜지 못했다: %s", exc)
            return [], []
        taken = {a.title for a in kept}
        angles = [a for a in angles if a.title not in taken and self._violates(a.text()) is None][:need]
        try:
            screened = await self._usage.screen(self._broadcast, angles)
        except _SOURCE_ERRORS as exc:
            log.warning("다시 짠 갈래를 다른 방송과 견주지 못했다: %r", exc)
            return angles, []
        self._vectors.update(screened.vectors)
        return screened.kept, screened.reused

    async def _aired_elsewhere(self) -> list[str]:
        """주제와 가까운, 다른 방송에서 다룬 갈래의 제목. 기록을 읽지 못하면 없이 간다."""
        if self._usage is None:
            return []
        try:
            return await self._usage.nearby_titles(self._broadcast, self._topic)
        except _SOURCE_ERRORS as exc:
            log.warning("다른 방송의 갈래를 읽지 못했다: %r", exc)
            return []

    async def _references(self) -> list:
        """오늘 주제와 가까운 이야깃거리 — 다른 방송이 쓴 것은 뺀다. 검색이 실패하거나 늦으면 없이 간다."""
        if self._bank is None:
            return []
        k = min(MAX_REFERENCES, self._count * REFERENCES_PER_ANGLE)
        try:
            # 다른 방송이 쓴 것을 알 수 없으면 참고 자료 없이 간다 — 모르고 주면 같은 이야기를 또 한다
            used = self._usage.used_items(self._broadcast) if self._usage else set()
            # 늦어도 검색을 끊지 않는다 — 뒤에서 벡터 캐시를 끝까지 채워야 다음 방송이 쓴다
            search = asyncio.ensure_future(
                self._bank.search(self._topic, fmt=self._fmt, persona=self._persona, k=k, exclude=used))
            search.add_done_callback(lambda task: task.cancelled() or task.exception())  # 버린 결과의 예외를 거둔다
            found = await asyncio.wait_for(asyncio.shield(search), SEARCH_WAIT_SEC)
        except _SOURCE_ERRORS as exc:
            log.warning("이야깃거리 검색 실패, 참고 자료 없이 갈래를 짠다: %r", exc)
            if self._telemetry is not None:
                self._telemetry.log_talk_bank(self._topic, None, error=repr(exc)[:200])
            return []
        if self._telemetry is not None:
            self._telemetry.log_talk_bank(self._topic, found)
        return [hit.item for hit in found.hits]

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
            if self._usage is not None:
                self._usage.record(self._broadcast, self._topic, angle, self._vectors.get(angle.title),
                                   station_id=self._station_id)
            if self._telemetry is not None:
                self._telemetry.log_angle(angle, len(self.used), len(self.used) + len(self._angles))
            state, points = NEW, self._remaining(n) or ()
        return Angle(self.current.title, points, self.current.source), state

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
