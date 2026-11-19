"""가상 청취자 — 방송에 나간 멘트를 듣고 성격에 따라 채팅을 치는 AI 청취자 (config/listeners.yaml).

정해진 시각에 정해진 글을 보내는 입력(inputs, chatter)은 DJ의 말에 반응하지 않는다. 가상 청취자는 청취자가
실제로 듣는 시각(재생 시작 + 방송 지연)에 그 멘트를 "들은" 것으로 보고, 타이핑 시간을 더해 글을 보낸다.
엔진이 미리 만들어 둔 멘트는 볼 수 없다.

칠지 말지는 확률로, 무엇을 칠지는 LLM으로 정한다. 멘트마다 LLM을 부르지 않으려는 것이고, 같은 seed면 같은
멘트에서 반응하게 하려는 것이다(글 자체는 LLM이라 회차마다 다르다).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..domain import Prompt
from ..pipeline.llm import LlmError
from ..prompt_data import DATA_RULES, data_block

log = logging.getLogger(__name__)

PASS = "PASS"  # LLM이 "지금은 쓸 말이 없다"고 답하는 말
HEARD = "방금까지 들은 방송"
MINE = "내가 보낸 글"
STATION = "방송 정보"
MAX_HEARD = 6  # 프롬프트에 넣는 최근에 들은 것의 수
MAX_MINE = 5
MAX_BODY_CHARS = 120
ACTIONS = ("chat", "message", "song_request")
# 묻는 말을 들으면 평소보다 답하기 쉽다 [임시값] — 실제 채팅 로그 없이 정했다
ANSWER_BOOST = 2.5

_SYSTEM = ("너는 인터넷 라디오 방송을 듣고 있는 청취자다. 채팅창에 올릴 글만 출력한다. 설명, 따옴표, 머리말을 붙이지 않는다.\n"
           + DATA_RULES)


class Persona(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    group: Literal["basic", "stress"] = "basic"
    about: str
    speech_rate: float = Field(ge=0, le=1)
    song_rate: float = Field(default=0.0, ge=0, le=1)
    # DJ가 청취자에게 물은 멘트를 들었을 때 반응할 확률. 없으면 speech_rate의 ANSWER_BOOST배(1을 넘지 않는다)
    answer_rate: float | None = Field(default=None, ge=0, le=1)
    actions: dict[Literal["chat", "message", "song_request"], float] = Field(default_factory=lambda: {"chat": 1.0})
    stale: int = Field(default=0, ge=0)
    resend_rate: float = Field(default=0.0, ge=0, le=1)
    lines: list[str] = Field(default_factory=list)
    samples: list[str] = Field(default_factory=list)


class Roster(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    personas: dict[str, Persona]
    nicknames: list[str] = Field(min_length=1)


def load_roster(path: str | Path) -> Roster:
    with open(path, encoding="utf-8") as f:
        return Roster.model_validate(yaml.safe_load(f))


@dataclass
class Heard:
    kind: str  # speech | song
    text: str


@dataclass
class Sent:
    request_id: str
    kind: str
    body: str
    answered: bool = False  # DJ가 이 글을 방송에서 받았다 (그 멘트의 재생이 시작됐다)
    after: str = ""  # 이 글을 쓸 때 마지막으로 들은 것 — 무엇에 대한 반응인지 (심사의 재료)


@dataclass
class Outgoing:
    """청취자가 보내려는 글 하나 — 가짜 백엔드가 엔진에 넣는다."""

    listener: Listener
    kind: str
    body: str
    title: str | None = None
    artist: str | None = None


@dataclass
class Listener:
    id: str
    key: str  # persona의 열쇠 (config/listeners.yaml)
    persona: Persona
    nickname: str
    rng: random.Random
    heard: list[Heard] = field(default_factory=list)
    sent: list[Sent] = field(default_factory=list)
    busy: bool = False  # 글을 쓰는 중 — 그동안 들은 것에는 반응하지 않는다

    def wants_to_react(self, kind: str, text: str = "") -> bool:
        rate = self.persona.song_rate if kind == "song" else self.persona.speech_rate
        if asks_listeners(kind, text):
            answer = self.persona.answer_rate
            rate = answer if answer is not None else min(1.0, rate * ANSWER_BOOST)
        return not self.busy and self.rng.random() < rate

    def pick_action(self) -> str:
        kinds, weights = zip(*self.persona.actions.items(), strict=True)
        return self.rng.choices(kinds, weights=weights)[0]


def asks_listeners(kind: str, text: str) -> bool:
    """DJ가 청취자에게 물은 멘트 — 물음표가 있는 말이다. 청취자의 질문을 옮긴 멘트("~냐고 물어보신 분")에는 물음표가 없다."""
    return kind == "speech" and ("?" in text or "？" in text)


def known_heard(listener: Listener) -> list[Heard]:
    """이 청취자가 안다고 여기는 최근 방송 — 맥락 혼란형(stale)은 최근 몇 멘트를 놓친 사람이다."""
    stale = listener.persona.stale
    known = listener.heard[:-stale] if stale else listener.heard
    return known[-MAX_HEARD:] or listener.heard[-1:]


def build_prompt(listener: Listener, station: dict, action: str) -> Prompt:
    persona = listener.persona
    known = known_heard(listener)
    lines = [
        f"너는 이런 청취자다: {persona.about}",
        "아래 방금까지 들은 방송의 맨 마지막 줄이 방금 들은 것이다. 이 청취자라면 지금 채팅창에 쓸 글을 한 줄 쓴다.",
        "- 실제 채팅처럼 짧고 입말로 쓴다. 맞춤법이 조금 틀려도 되고, ㅋㅋ나 ㅠㅠ를 써도 된다. 방송 작가처럼 매끈하게 쓰지 않는다.",
        "- 방송에서 들은 것만 안다. 듣지 않은 이야기를 아는 것처럼 쓰지 않는다.",
        "- 내가 보낸 글과 같은 말을 되풀이하지 않는다." if not persona.resend_rate else
        "- 내가 보낸 글에 DJ가 답하지 않았으면 다시 조를 수 있다.",
        f"- 이 청취자라도 지금은 쓸 말이 없으면 {PASS}라고만 쓴다.",
    ]
    if known and asks_listeners(known[-1].kind, known[-1].text):
        lines.append("- 방금 DJ가 청취자들에게 물었다. 이 청취자라면 그 물음에 뭐라고 답할지를 쓴다(성격은 그대로다).")
    if persona.samples:
        lines.append("- 이런 식의 글을 쓰는 사람이다(그대로 베끼지 않는다): " + " / ".join(persona.samples))
    if action == "message":
        lines.append("- 이번에는 채팅이 아니라 DJ가 읽어 주길 바라며 보내는 사연 문자다. 두세 문장으로, 200자 이내로 쓴다.")
    elif action == "song_request":
        lines.append("- 이번에는 신청곡이다. 실제로 있는 곡 하나를 골라 '곡 제목 | 아티스트 | 한마디' 형식으로 한 줄 쓴다. "
                     "한마디는 왜 듣고 싶은지 짧게.")
    lines += ["", data_block(STATION, "\n".join(f"{k}: {v}" for k, v in station.items() if v))]
    lines.append(data_block(HEARD, "\n".join(
        f"- {'(곡) ' if h.kind == 'song' else ''}{h.text}" for h in known)))
    if listener.sent:
        lines.append(data_block(MINE, "\n".join(
            f"- {s.body}{' (DJ가 방송에서 받아 줌)' if s.answered else ''}" for s in listener.sent[-MAX_MINE:])))
    return Prompt(system=_SYSTEM, user="\n".join(lines))


class SampleWriter:
    """더미 청취자 글 — persona의 말투 예시에서 고른다. API 키 없이 실행기와 테스트를 돌릴 때 쓴다."""

    def __init__(self, seed: int = 0):
        self._rng = random.Random(seed)

    async def write(self, listener: Listener, station: dict, action: str) -> str:
        if action == "song_request":
            return "첫눈 오는 밤 | 가상의 가수 | 눈 오는 날 생각나서요"
        return self._rng.choice(listener.persona.samples) if listener.persona.samples else PASS


class LlmWriter:
    """LLM이 쓰는 청취자 글. 제공자 장애나 거부는 "쓸 말 없음"으로 본다 — 청취자 하나가 조용해질 뿐이다."""

    def __init__(self, llm, max_tokens: int = 300):
        self._llm = llm
        self.max_tokens = max_tokens
        self.calls = 0
        self.failures = 0

    async def write(self, listener: Listener, station: dict, action: str) -> str:
        self.calls += 1
        try:
            return await self._llm.complete(build_prompt(listener, station, action), max_tokens=self.max_tokens)
        except (LlmError, OSError) as exc:
            self.failures += 1
            log.warning("청취자 %s의 글을 만들지 못했다: %s", listener.id, exc)
            return PASS


def parse_outgoing(listener: Listener, action: str, raw: str) -> Outgoing | None:
    """LLM 출력에서 보낼 글을 꺼낸다. 쓸 말이 없다고 했거나 형식이 틀리면 None."""
    text = " ".join(raw.strip().strip('"“”').split())
    if not text or text.upper().startswith(PASS):
        return None
    if action == "song_request":
        parts = [p.strip() for p in text.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return None
        comment = parts[2][:60] if len(parts) > 2 else ""
        return Outgoing(listener, action, comment, title=parts[0][:80], artist=parts[1][:60])
    limit = 200 if action == "message" else MAX_BODY_CHARS
    return Outgoing(listener, action, text[:limit])


class Audience:
    """방송 하나의 가상 청취자들. 가짜 백엔드가 재생을 시작할 때마다 알려 주면, 들은 뒤 글을 보낸다."""

    def __init__(self, roster: Roster, members: list[tuple[str, int]], *, writer, station: dict, seed: int,
                 delay_sec: float, send, typing_sec: tuple[float, float] = (3.0, 10.0)):
        self.station = station
        self._writer = writer
        self._delay = delay_sec  # 방송 지연 — 청취자는 재생 시작보다 이만큼 늦게 듣는다
        self._send = send  # async (Outgoing) -> request_id | None
        self._typing = typing_sec
        self._tasks: set[asyncio.Task] = set()
        self._by_request: dict[str, tuple[Listener, Sent]] = {}
        self.listeners: list[Listener] = []
        n = 0
        for key, count in members:
            if key not in roster.personas:
                raise ValueError(f"모르는 청취자 유형: {key} (config/listeners.yaml)")
            for i in range(count):
                self.listeners.append(Listener(
                    id=f"{key}{i + 1}", key=key, persona=roster.personas[key],
                    nickname=roster.nicknames[n % len(roster.nicknames)],
                    # 유형 이름을 seed에 섞는다 — 아니면 상황이 달라도 같은 자리의 청취자가 같은 멘트에서 반응한다
                    rng=random.Random(f"{seed}:{key}:{i}")))
                n += 1

    def on_play(self, kind: str, text: str | None, duration_sec: float, request_ids: tuple[str, ...] = ()) -> None:
        """재생이 시작됐다. kind는 speech 또는 song. request_ids는 이 멘트가 받은 청취자 글이다."""
        if not text:
            return
        task = asyncio.create_task(self._hear(kind, text, duration_sec, request_ids))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _hear(self, kind: str, text: str, duration_sec: float, request_ids: tuple[str, ...]) -> None:
        await asyncio.sleep(self._delay)  # 이제 청취자에게 들리기 시작한다
        for rid in request_ids:
            if rid in self._by_request:
                self._by_request[rid][1].answered = True
        for listener in self.listeners:
            listener.heard.append(Heard(kind, text))
            del listener.heard[:-40]
            if listener.wants_to_react(kind, text):
                listener.busy = True
                # 곡은 듣는 중에, 멘트는 절반쯤 듣고부터 반응한다
                wait = duration_sec * (listener.rng.uniform(0.1, 0.6) if kind == "song"
                                       else listener.rng.uniform(0.5, 1.0))
                task = asyncio.create_task(self._react(listener, wait))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _react(self, listener: Listener, wait: float) -> None:
        try:
            await asyncio.sleep(wait)
            persona = listener.persona
            known = known_heard(listener)
            after = (("(곡) " if known[-1].kind == "song" else "") + known[-1].text) if known else ""
            if persona.resend_rate and listener.sent and listener.rng.random() < persona.resend_rate:
                last = listener.sent[-1]
                outgoing = Outgoing(listener, "chat", last.body) if last.kind == "chat" else None
            elif persona.lines:
                outgoing = Outgoing(listener, "chat", listener.rng.choice(persona.lines))
            else:
                action = listener.pick_action()
                started = time.monotonic()
                raw = await self._writer.write(listener, self.station, action)
                outgoing = parse_outgoing(listener, action, raw)
                # LLM을 기다린 시간도 타이핑 시간으로 친다
                typing = listener.rng.uniform(*self._typing) - (time.monotonic() - started)
                if typing > 0:
                    await asyncio.sleep(typing)
            if outgoing is None:
                return
            request_id = await self._send(outgoing)
            if request_id:
                sent = Sent(request_id, outgoing.kind, outgoing.body or outgoing.title or "", after=after)
                listener.sent.append(sent)
                self._by_request[request_id] = (listener, sent)
        finally:
            listener.busy = False

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def summary(self) -> list[dict]:
        """청취자별로 보낸 글과 DJ가 받은 글 — 결과 보고서와 심사의 재료다."""
        return [{"id": l.id, "persona": l.key, "name": l.persona.name, "nickname": l.nickname,
                 "sent": [{"request_id": s.request_id, "kind": s.kind, "body": s.body, "answered": s.answered,
                           "after": s.after} for s in l.sent]}
                for l in self.listeners]
