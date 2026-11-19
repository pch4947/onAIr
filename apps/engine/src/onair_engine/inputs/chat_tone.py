"""채팅 성격 판정 — 불만은 받지 않고, 농담은 농담으로 받는다 (CORNER_DESIGN 5.1.4절).

규칙으로는 가릴 수 없다. "핫팩 먹어도 돼요?"는 물음표가 붙은 질문이고, "제 채팅은 또 씹으시네"에는 걸러 낼
낱말이 없다. 그래서 채팅이 들어올 때 LLM에 한 번 묻는다. 판정이 늦거나 실패하면 보통 채팅으로 본다 —
판정 때문에 채팅이 방송에서 빠지면 안 된다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from ..domain import TONE_COMPLAINT, TONE_JOKE, TONE_PLAIN, ListenerRequest, Prompt
from ..pipeline.llm import LlmError
from ..prompt_data import CHAT, DATA_RULES, TOPIC, data_block

log = logging.getLogger(__name__)

MARKER = "채팅 성격 판정"  # 더미 LLM이 이 요청을 알아보는 구절 — 지우거나 다른 지시에 쓰지 않는다
PLAIN, JOKE, COMPLAINT = TONE_PLAIN, TONE_JOKE, TONE_COMPLAINT
TONES = (PLAIN, JOKE, COMPLAINT)
MAX_BODY_CHARS = 200

_SYSTEM = ("너는 라디오 방송 편성 보조다. 청취자 채팅 한 건이 어떤 성격의 말인지 판정한다.\n"
           + DATA_RULES)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def build_prompt(topic: str, body: str) -> Prompt:
    lines = [
        f"{MARKER}: 아래 청취자 채팅이 셋 가운데 어느 것인지 고른다.",
        (f"- {COMPLAINT}: 방송 진행, DJ, 선곡, 다른 청취자를 향한 불만·비난·비꼼, 또는 자기 글을 읽어 주지 "
         "않는다는 항의. 정중한 부탁이나 신청, 아쉽다는 말은 여기에 넣지 않는다."),
        # 웃는 표시만 보고 농담으로 본 적이 있다 — "호빵 냄새는 진짜 못 참죠 ㅋㅋㅋ"(시험 실행 20261119-130733)
        (f"- {JOKE}: 사실이 아니거나 말이 안 되는 것을 일부러 말한 장난, DJ를 놀리는 말. 사실로 받아 진지하게 답하면 "
         "어색해지는 말이다. 웃는 표시(ㅋㅋ, ㅎㅎ)가 붙었다고 농담인 것은 아니다 — 웃으며 한 맞장구, 자기 이야기, "
         "DJ의 질문에 대한 답은 여기에 넣지 않는다."),
        f"- {PLAIN}: 그 밖의 모든 채팅. 어느 쪽인지 분명하지 않으면 이것이다.",
        f'- 출력은 JSON 하나만: {{"tone": "{PLAIN}"}}',
        "",
        data_block(TOPIC, topic),
        data_block(CHAT, body[:MAX_BODY_CHARS]),
    ]
    return Prompt(system=_SYSTEM, user="\n".join(lines))


def parse_tone(text: str) -> str:
    """판정 결과. 해석할 수 없으면 ValueError."""
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError("JSON이 없습니다")
    data = json.loads(match.group(0))
    tone = data.get("tone") if isinstance(data, dict) else None
    if tone not in TONES:
        raise ValueError(f"모르는 성격: {tone!r}")
    return tone


def dummy_tone_json(prompt: Prompt) -> str:
    """더미 LLM 응답 — 모든 채팅을 보통 채팅으로 본다."""
    return json.dumps({"tone": PLAIN})


class ChatToneJudge:
    def __init__(self, llm, topic: str, *, telemetry=None, max_tokens: int = 2000,
                 timeout_sec: float = 6.0, concurrency: int = 4):
        self._llm = llm
        self._topic = topic
        self._telemetry = telemetry
        self.max_tokens = max_tokens
        self.timeout_sec = timeout_sec  # 넘기면 보통 채팅으로 본다 — 인용이 판정을 기다리다 늦어지지 않게
        self._slots = asyncio.Semaphore(concurrency)

    async def judge(self, req: ListenerRequest) -> str:
        """채팅 한 건의 성격. 판정하지 못하면 보통 채팅(PLAIN)이다."""
        tone, method = PLAIN, "llm"
        try:
            async with asyncio.timeout(self.timeout_sec):
                async with self._slots:
                    text = await self._llm.complete(build_prompt(self._topic, req.body),
                                                    max_tokens=self.max_tokens)
            tone = parse_tone(text)
        except TimeoutError:
            method = "timeout"
        except (LlmError, ValueError, OSError) as exc:
            log.warning("채팅 성격 판정 실패, 보통 채팅으로 본다: %s", exc)
            method = "failed"
        if self._telemetry is not None:
            self._telemetry.log_chat_tone(req, tone, method)
        return tone
