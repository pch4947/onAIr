"""게시판 사연 관련도 판정 — 오늘 주제와 맞는 사연만 사연 코너 후보로 남긴다 (CORNER_DESIGN 5.4절).

게시글에는 태그가 없고 단어 일치로는 "겨울 간식"과 "붕어빵"처럼 뜻이 같은 사연을 놓친다.
그래서 받은 사연을 모아 LLM에 한 번에 묻고, 실패하면 키워드 일치로 대신한다.
판정은 사연마다 한 번만 하고 결과를 남긴다 — 같은 방송을 다시 분석할 때 LLM을 다시 부르지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from ..domain import ListenerRequest, Prompt
from ..pipeline.llm import LlmError
from ..prompt_data import DATA_RULES, TOPIC, data_block
from .classify import keywords
from .stories import StoryPool

log = logging.getLogger(__name__)

MARKER = "사연 관련도 판정"  # 더미 LLM이 이 요청을 알아보는 구절 — 지우거나 다른 지시에 쓰지 않는다
CANDIDATE = "사연 후보"
MAX_BODY_CHARS = 500  # [예시] 백엔드도 500자 넘는 글은 보내지 않는다 (#196)
MAX_BATCH = 30  # [예시] 계약 3.5절의 후보 상한과 같다

_SYSTEM = ("너는 라디오 방송 편성 보조다. 청취자 사연이 오늘 방송 주제와 어울리는지 판정한다.\n"
           + DATA_RULES)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def build_prompt(topic: str, stories: list[ListenerRequest]) -> Prompt:
    lines = [
        f"{MARKER}: 아래 사연 후보가 오늘 방송 주제와 관련 있는지 판정한다.",
        "- 주제를 직접 다루거나, 주제로 자연스럽게 이야기를 이어 갈 수 있으면 관련 있다.",
        "- 주제와 억지로 엮어야 하면 관련 없다.",
        "- 사연 번호는 각 후보 블록 첫 줄의 번호다.",
        '- 출력은 JSON 하나만: {"relevant": [관련 있는 사연 번호]}',
        "",
        data_block(TOPIC, topic),
    ]
    for i, story in enumerate(stories, 1):
        lines.append(data_block(CANDIDATE, f"번호: {i}\n{story.body[:MAX_BODY_CHARS]}"))
    return Prompt(system=_SYSTEM, user="\n".join(lines))


def parse_relevant(text: str, count: int) -> set[int]:
    """관련 있는 사연 번호(1부터). 범위 밖 번호는 버린다. 해석할 수 없으면 ValueError."""
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError("JSON이 없습니다")
    data = json.loads(match.group(0))
    numbers = data.get("relevant") if isinstance(data, dict) else None
    if not isinstance(numbers, list):
        raise ValueError("relevant 배열이 없습니다")  # noqa: TRY004 — 호출자는 ValueError 하나로 받는다
    return {n for n in numbers if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= count}


def dummy_judgement_json(prompt: Prompt) -> str:
    """더미 LLM 응답 — 모든 후보를 관련 있다고 본다. API 키 없이도 사연 코너가 열리게 한다."""
    count = prompt.user.count(f'<data label="{CANDIDATE}">')
    return json.dumps({"relevant": list(range(1, count + 1))})


class StoryJudge:
    def __init__(self, llm, topic: str, pool: StoryPool, *, telemetry=None,
                 max_tokens: int = 4000, batch_delay_sec: float = 1.0):
        self._llm = llm
        self._topic = topic
        self._topic_words = keywords(topic)
        self._pool = pool
        self._telemetry = telemetry
        self.max_tokens = max_tokens
        self.batch_delay_sec = batch_delay_sec  # 백엔드는 후보를 연달아 보낸다 — 잠깐 모아서 한 번에 묻는다

    async def run(self) -> None:
        """방송 중 새로 온 사연을 판정한다. 취소될 때까지 돈다."""
        while True:
            await self._pool.arrived.wait()
            self._pool.arrived.clear()
            await asyncio.sleep(self.batch_delay_sec)
            await self.judge_pending()

    async def judge_pending(self) -> None:
        while batch := self._pool.unjudged()[:MAX_BATCH]:
            verdicts, method = await self._judge(batch)
            self._pool.settle(verdicts)
            if self._telemetry is not None:
                for story in batch:
                    self._telemetry.log_story_judgement(story.request_id, verdicts[story.request_id],
                                                        method)

    async def _judge(self, batch: list[ListenerRequest]) -> tuple[dict[str, bool], str]:
        try:
            text = await self._llm.complete(build_prompt(self._topic, batch), max_tokens=self.max_tokens)
            relevant = parse_relevant(text, len(batch))
        except (LlmError, ValueError, OSError) as exc:  # 제공자 장애·해석 실패 — 규칙으로 대신한다
            log.warning("사연 관련도 판정 실패, 키워드 일치로 대신한다: %s", exc)
            return {s.request_id: self._keyword_match(s) for s in batch}, "keyword"
        return {s.request_id: i in relevant for i, s in enumerate(batch, 1)}, "llm"

    def _keyword_match(self, story: ListenerRequest) -> bool:
        return bool((keywords(story.body) | keywords(" ".join(story.tags))) & self._topic_words)
