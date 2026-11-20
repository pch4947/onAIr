"""채팅 성격 판정 — 불만은 받지 않고, 농담은 농담으로 받는다 (CORNER_DESIGN 5.1.4절).

같은 호출에서 그 채팅이 혼자 뜻이 통하는 글인지도 묻는다 (5.1.5절). "완전 공감이에요"는 방금 들은 말이 있어야
뜻이 통해서, 곡이 지난 뒤에 읽으면 무엇에 공감했다는 것인지 알 수 없다. 길이와 감탄 낱말로 가르는 규칙은
"와 진짜 상상만 해도 너무 따뜻해요 ㅠㅠ"(21자)를 혼자 통하는 글로 봤다.

규칙으로는 가릴 수 없다. "핫팩 먹어도 돼요?"는 물음표가 붙은 질문이고, "제 채팅은 또 씹으시네"에는 걸러 낼
낱말이 없다. 그래서 채팅이 들어올 때 LLM에 한 번 묻는다. 판정이 늦거나 실패하면 보통 채팅으로 본다 —
판정 때문에 채팅이 방송에서 빠지면 안 된다.

같은 호출에서 그 채팅이 오늘의 주제에 관한 글인지도 묻는다 — 엔딩 돌아보기가 주제와 관련 있는 글만 다시 꺼내게
하려는 것이다(사용자 결정 2026-11-20). 실제 방송의 엔딩이 "노래가 끊겼어요", "왜 말씀이 없으세요"를 돌아봤다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from ..domain import (
    ABOUT_SELF,
    ABOUT_SONG,
    ABOUT_TALK,
    TONE_COMPLAINT,
    TONE_JOKE,
    TONE_PLAIN,
    ListenerRequest,
    Prompt,
)
from ..pipeline.llm import LlmError
from ..prompt_data import CHAT, DATA_RULES, HEARD, TOPIC, data_block

log = logging.getLogger(__name__)

MARKER = "채팅 성격 판정"  # 더미 LLM이 이 요청을 알아보는 구절 — 지우거나 다른 지시에 쓰지 않는다
PLAIN, JOKE, COMPLAINT = TONE_PLAIN, TONE_JOKE, TONE_COMPLAINT
TONES = (PLAIN, JOKE, COMPLAINT)
ABOUTS = (ABOUT_SELF, ABOUT_TALK, ABOUT_SONG)
ON_TOPIC, OFF_TOPIC = "on", "off"
MAX_BODY_CHARS = 200
MAX_HEARD_CHARS = 400

_SYSTEM = ("너는 라디오 방송 편성 보조다. 청취자 채팅 한 건이 어떤 성격의 말인지 판정한다.\n"
           + DATA_RULES)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def build_prompt(topic: str, body: str, heard: str | None = None) -> Prompt:
    lines = [
        f"{MARKER}: 아래 청취자 채팅에 대해 세 가지를 고른다.",
        "첫째(tone), 어떤 성격의 말인가.",
        (f"- {COMPLAINT}: 방송 진행, DJ, 선곡, 다른 청취자를 향한 불만·비난·비꼼, 또는 자기 글을 읽어 주지 "
         "않는다는 항의. 정중한 부탁이나 신청, 아쉽다는 말은 여기에 넣지 않는다."),
        # 웃는 표시만 보고 농담으로 본 적이 있다 — "호빵 냄새는 진짜 못 참죠 ㅋㅋㅋ"(시험 실행 20261119-130733)
        (f"- {JOKE}: 사실이 아니거나 말이 안 되는 것을 일부러 말한 장난, DJ를 놀리는 말. 사실로 받아 진지하게 답하면 "
         "어색해지는 말이다. 웃는 표시(ㅋㅋ, ㅎㅎ)가 붙었다고 농담인 것은 아니다 — 웃으며 한 맞장구, 자기 이야기, "
         "DJ의 질문에 대한 답은 여기에 넣지 않는다."),
        f"- {PLAIN}: 그 밖의 모든 채팅. 어느 쪽인지 분명하지 않으면 이것이다.",
        ("둘째(about), 이 채팅만 따로 읽어 줘도 뜻이 통하는가. 곡이 한 곡 지난 뒤에 DJ가 이 채팅만 읽어 준다고 "
         "생각하고 고른다."),
        (f"- {ABOUT_SELF}: 듣는 사람이 무슨 이야기인지 안다. 자기 경험이나 사연, 무엇을 묻는지가 글에 다 들어 있는 "
         "질문, 부탁이나 신청. 맞장구로 시작했더라도 자기 이야기가 한 문장 온전히 들어 있으면 이것이다."),
        (f"- {ABOUT_TALK}: 방금 방송에서 들은 말이 있어야 뜻이 통한다. 맞장구, 감탄, 방금 한 말을 되묻는 말, "
         "DJ가 던진 질문에 대한 짧은 답, 자기 글을 읽어 준 데 대한 반응. 무엇에 공감하는지, 무엇을 두고 묻거나 "
         "답하는지가 글에 없으면 이것이다."),
        f"- {ABOUT_SONG}: 방송에 나가는 곡이나 그 제목·가수에 대한 감상.",
        f"- 어느 쪽인지 분명하지 않으면 {ABOUT_SELF}다.",
        ("셋째(topic), 아래 주제에 관한 글인가. 방송을 마치며 DJ가 오늘 나눈 이야기를 돌아볼 때 다시 꺼낼 만한 "
         "글인지로 고른다."),
        f"- {ON_TOPIC}: 주제에 관한 자기 경험이나 생각, 사연이 들어 있다.",
        # "소리·끊김·진행 같은 방송 상태에 대한 말"을 예로 들었더니 첫째 판정이 달라졌다 — "소리가 안 들려요"가
        # 불만 10번 중 9번에서 0번이 됐다(2026-11-20, var/tone_focus.py). 예를 빼고 넓게만 적는다
        f"- {OFF_TOPIC}: 인사, 맞장구나 감탄뿐인 글, 곡 감상이나 신청, 그 밖에 주제와 상관없는 모든 글.",
        f"- 어느 쪽인지 분명하지 않으면 {OFF_TOPIC}다.",
        f'출력은 JSON 하나만: {{"tone": "{PLAIN}", "about": "{ABOUT_SELF}", "topic": "{ON_TOPIC}"}}',
        "",
        data_block(TOPIC, topic),
    ]
    if heard:
        lines += ["이 채팅을 보낸 사람은 아래 것을 듣고 있었다.", data_block(HEARD, heard[:MAX_HEARD_CHARS])]
    lines.append(data_block(CHAT, body[:MAX_BODY_CHARS]))
    return Prompt(system=_SYSTEM, user="\n".join(lines))


def _parse(text: str) -> dict:
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError("JSON이 없습니다")
    data = json.loads(match.group(0))
    if not isinstance(data, dict) or data.get("tone") not in TONES:
        raise ValueError(f"모르는 성격: {data.get('tone') if isinstance(data, dict) else data!r}")
    return data


def parse_judgement(text: str) -> tuple[str, str | None]:
    """(성격, 기대는 것). 성격을 해석할 수 없으면 ValueError. 기대는 것은 없거나 모르는 값이면 None이다."""
    data = _parse(text)
    about = data.get("about")
    return data["tone"], about if about in ABOUTS else None


def parse_on_topic(text: str) -> bool | None:
    """주제에 관한 글인가. 답이 없거나 모르는 값이면 None — 판정하지 않은 것으로 본다."""
    return {ON_TOPIC: True, OFF_TOPIC: False}.get(_parse(text).get("topic"))


def parse_tone(text: str) -> str:
    """판정 결과 가운데 성격. 해석할 수 없으면 ValueError."""
    return parse_judgement(text)[0]


def dummy_tone_json(prompt: Prompt) -> str:
    """더미 LLM 응답 — 모든 채팅을 보통 채팅으로 본다. 기대는 것은 답하지 않아 규칙 분류를 따른다."""
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
        """채팅 한 건의 성격. 판정하지 못하면 보통 채팅(PLAIN)이다.

        기대는 것(req.about)도 함께 적는다 — 판정하지 못하면 None으로 두어 규칙 분류를 따르게 한다.
        주제에 관한 글인지(req.on_topic)도 적는다 — 엔딩 돌아보기가 본다 (inputs/callbacks.py).
        """
        tone, method = PLAIN, "llm"
        try:
            async with asyncio.timeout(self.timeout_sec):
                async with self._slots:
                    text = await self._llm.complete(build_prompt(self._topic, req.body, req.heard),
                                                    max_tokens=self.max_tokens)
            tone, req.about = parse_judgement(text)
            req.on_topic = parse_on_topic(text) if self._topic else None  # 주제 없는 방송은 판정하지 않는다
        except TimeoutError:
            method = "timeout"
        except (LlmError, ValueError, OSError) as exc:
            log.warning("채팅 성격 판정 실패, 보통 채팅으로 본다: %s", exc)
            method = "failed"
        if self._telemetry is not None:
            self._telemetry.log_chat_tone(req, tone, method)
        return tone
