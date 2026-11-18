"""가상 청취자 — 방송에 나간 멘트를 듣고 성격에 따라 채팅을 친다 (scenario/listeners.py)."""
import asyncio
import random
from pathlib import Path

import pytest

from onair_engine.pipeline.llm import LlmError
from onair_engine.scenario.listeners import (
    HEARD,
    MINE,
    PASS,
    Audience,
    Heard,
    Listener,
    LlmWriter,
    Outgoing,
    SampleWriter,
    Sent,
    build_prompt,
    load_roster,
    parse_outgoing,
)
from onair_engine.scenario.spec import load_scenario

ROOT = Path(__file__).resolve().parent.parent
ROSTER = load_roster(ROOT / "config" / "listeners.yaml")
STATION = {"DJ 이름": "새벽", "방송 컨셉": "심야 스터디 라디오", "오늘의 주제": "겨울 간식"}


def _listener(key: str, seed: int = 1) -> Listener:
    return Listener(id=f"{key}1", key=key, persona=ROSTER.personas[key], nickname="밤샘러", rng=random.Random(seed))


class Always:
    """늘 같은 글을 쓰는 작가. 받은 요청을 기록한다."""

    def __init__(self, text: str = "맞아요 맞아요"):
        self.text = text
        self.seen: list[tuple[str, str, list[str]]] = []

    async def write(self, listener, station, action):
        self.seen.append((listener.id, action, [h.text for h in listener.heard]))
        return self.text


def _audience(members, writer, sent, **kwargs) -> Audience:
    async def send(out: Outgoing):
        sent.append(out)
        return f"aud_{len(sent)}"

    return Audience(ROSTER, members, writer=writer, station=STATION, seed=3, delay_sec=0.02, send=send,
                    typing_sec=(0.0, 0.0), **kwargs)


# ── 설정 ──────────────────────────────────────────────────────────────

def test_roster_has_basic_and_stress_personas_and_scenarios_use_known_ones():
    groups = {key: p.group for key, p in ROSTER.personas.items()}
    assert sum(g == "basic" for g in groups.values()) == 7 and sum(g == "stress" for g in groups.values()) == 5
    assert ROSTER.personas["inappropriate"].lines, "부적절한 발언은 LLM에게 쓰게 하지 않고 정해 둔 글에서 고른다"
    for path in sorted((ROOT / "config" / "scenarios").glob("aud_*.yaml")):
        spec = load_scenario(path)
        assert all(item.persona in ROSTER.personas for item in spec.listeners), path.name
    assert load_scenario(ROOT / "config" / "scenarios" / "aud_0.yaml").listeners == []
    with pytest.raises(ValueError, match="모르는 청취자 유형"):
        _audience([("nobody", 1)], Always(), [])


# ── 프롬프트와 해석 ────────────────────────────────────────────────────

def test_prompt_shows_only_what_the_listener_heard_and_what_they_sent():
    listener = _listener("question")
    listener.heard = [Heard("speech", "붕어빵은 꼬리부터 먹는 분이 많죠."), Heard("song", "첫눈 오는 밤 - 가상의 가수")]
    listener.sent = [Sent("aud_1", "chat", "DJ님은 뭐부터 드세요?", answered=True)]
    user = build_prompt(listener, STATION, "chat").user
    assert f'<data label="{HEARD}">\n- 붕어빵은 꼬리부터 먹는 분이 많죠.\n- (곡) 첫눈 오는 밤 - 가상의 가수\n</data>' in user
    assert f'<data label="{MINE}">\n- DJ님은 뭐부터 드세요? (DJ가 방송에서 받아 줌)\n</data>' in user
    assert "오늘의 주제: 겨울 간식" in user and PASS in user
    assert "신청곡" not in user and "곡 제목 | 아티스트 | 한마디" in build_prompt(listener, STATION, "song_request").user


def test_confused_listener_does_not_know_the_latest_ments():
    listener = _listener("confused")
    listener.heard = [Heard("speech", f"{n}번째 이야기") for n in range(1, 8)]
    user = build_prompt(listener, STATION, "chat").user
    assert "3번째 이야기" in user and "4번째 이야기" not in user, "네 멘트 전까지만 들은 것으로 친다"


@pytest.mark.parametrize(("action", "raw", "expected"), [
    ("chat", '"맞아요 맞아요"', ("chat", "맞아요 맞아요", None)),
    ("chat", "PASS", None),
    ("chat", "  ", None),
    ("message", "회사 앞에 호떡 노점이 생겼어요.\n내일도 가려고요.", ("message", "회사 앞에 호떡 노점이 생겼어요. 내일도 가려고요.", None)),
    ("song_request", "밤편지 | 아이유 | 잠이 안 와서요", ("song_request", "잠이 안 와서요", "밤편지")),
    ("song_request", "밤편지 틀어 주세요", None),  # 형식이 틀리면 보내지 않는다
])
def test_parse_outgoing(action, raw, expected):
    out = parse_outgoing(_listener("music"), action, raw)
    assert (out and (out.kind, out.body, out.title)) == expected


def test_llm_failure_only_silences_the_listener():
    class Down:
        async def complete(self, prompt, *, max_tokens):
            raise LlmError("gemini 503")

    writer = LlmWriter(Down())
    assert asyncio.run(writer.write(_listener("active"), STATION, "chat")) == PASS
    assert (writer.calls, writer.failures) == (1, 1)


def test_sample_writer_needs_no_api_key():
    writer = SampleWriter(1)
    listener = _listener("active")
    assert asyncio.run(writer.write(listener, STATION, "chat")) in listener.persona.samples
    assert parse_outgoing(listener, "song_request", asyncio.run(writer.write(listener, STATION, "song_request")))


# ── 듣고 반응하기 ──────────────────────────────────────────────────────

def test_listener_reacts_only_after_hearing_and_never_sees_unaired_ments():
    sent: list[Outgoing] = []
    writer = Always()

    async def scenario():
        audience = _audience([("spam", 1)], writer, sent)
        audience.listeners[0].rng = random.Random(0)
        audience.listeners[0].persona = audience.listeners[0].persona.model_copy(
            update={"speech_rate": 1.0, "resend_rate": 0.0})
        audience.on_play("speech", "첫 멘트", 0.02)
        assert audience.listeners[0].heard == [], "방송 지연이 지나기 전에는 들리지 않는다"
        await asyncio.sleep(0.15)
        audience.on_play("speech", None, 0.02)  # 대본을 모르는 멘트(고정 문구)는 들은 것으로 치지 않는다
        await asyncio.sleep(0.1)
        await audience.close()
        return audience

    audience = asyncio.run(scenario())
    assert [o.body for o in sent] == ["맞아요 맞아요"] and writer.seen == [("spam1", "chat", ["첫 멘트"])]
    assert audience.summary()[0]["sent"] == [
        {"request_id": "aud_1", "kind": "chat", "body": "맞아요 맞아요", "answered": False, "after": "첫 멘트"}]


def test_listener_learns_that_the_dj_took_their_chat():
    sent: list[Outgoing] = []

    async def scenario():
        audience = _audience([("active", 1)], Always(), sent)
        who = audience.listeners[0]
        who.persona = who.persona.model_copy(update={"speech_rate": 1.0})
        audience.on_play("speech", "첫 멘트", 0.01)
        await asyncio.sleep(0.15)
        who.persona = who.persona.model_copy(update={"speech_rate": 0.0})
        audience.on_play("speech", "맞다고 해 주신 분이 계시네요", 0.01, ("aud_1", "남의_글"))
        await asyncio.sleep(0.1)
        await audience.close()
        return who

    who = asyncio.run(scenario())
    assert [s.answered for s in who.sent] == [True]
    assert "(DJ가 방송에서 받아 줌)" in build_prompt(who, STATION, "chat").user


def test_scripted_and_resending_personas_do_not_call_the_writer():
    sent: list[Outgoing] = []
    writer = Always("새 글")

    async def scenario():
        audience = _audience([("inappropriate", 1), ("spam", 1)], writer, sent)
        bad, spam = audience.listeners
        bad.persona = bad.persona.model_copy(update={"speech_rate": 1.0})
        spam.persona = spam.persona.model_copy(update={"speech_rate": 1.0, "resend_rate": 1.0})
        spam.sent.append(Sent("aud_0", "chat", "제 거 읽어 주세요"))
        audience.on_play("speech", "멘트", 0.01)
        await asyncio.sleep(0.15)
        await audience.close()

    asyncio.run(scenario())
    bodies = {o.listener.key: o.body for o in sent}
    assert bodies["inappropriate"] in ROSTER.personas["inappropriate"].lines
    assert bodies["spam"] == "제 거 읽어 주세요" and writer.seen == [], "정해 둔 글과 되풀이는 LLM을 부르지 않는다"


def test_same_seed_reacts_to_the_same_ments():
    def reactions(seed: int) -> list[bool]:
        listener = _listener("active", seed)
        return [listener.wants_to_react("speech") for _ in range(30)]

    assert reactions(5) == reactions(5) and reactions(5) != reactions(6)
    quiet, active = _listener("quiet", 5), _listener("active", 5)
    assert sum(quiet.wants_to_react("speech") for _ in range(400)) < sum(active.wants_to_react("speech") for _ in range(400))
