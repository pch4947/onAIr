"""신청곡 판정 — 신청한 곡이 방의 장르와 분위기에 어울리는지 본다 (계약 6.4절)."""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from onair_engine.api import create_app
from onair_engine.domain import Prompt, StationConfig, StationProfile
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.inputs.song_fit import (
    MARKER,
    PASS_UNJUDGED,
    Fit,
    SongFitJudge,
    build_prompt,
    parse_fit,
)
from onair_engine.manager import EngineManager
from onair_engine.personas import PersonaDrafter
from onair_engine.pipeline.llm import DummyLlmClient, LlmError
from onair_engine.pipeline.safety import SafetyChecker
from onair_engine.prompt_data import GENRES, TRACK

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"
PROFILE = StationProfile(dj_name="새벽", tone="차분한 존댓말", concept="심야 스터디 라디오")
TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REJECT = json.dumps({"fit": False, "known": True, "reason": "시끄러운 메탈 곡이라 잔잔한 방송과 맞지 않습니다."},
                    ensure_ascii=False)


class Scripted:
    """정해 둔 답을 차례로 돌려준다. 받은 프롬프트를 기록한다. 예외를 넣으면 그 차례에 실패한다."""

    def __init__(self, *outputs, delay: float = 0.0):
        self.outputs = list(outputs)
        self.prompts: list[Prompt] = []
        self.delay = delay

    async def complete(self, prompt: Prompt, *, max_tokens: int) -> str:
        self.prompts.append(prompt)
        await asyncio.sleep(self.delay)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _judge(llm, genres=("발라드",), violates=lambda text: None, telemetry=None) -> SongFitJudge:
    return SongFitJudge(llm, PROFILE, list(genres), "수능 응원", violates=violates, telemetry=telemetry)


# ── 프롬프트와 해석 ────────────────────────────────────────────────────

def test_prompt_carries_genres_and_the_song_as_data():
    prompt = build_prompt(PROFILE, ["발라드", "인디"], "수능 응원", "[MV] 밤편지", "아이유")
    assert MARKER in prompt.user and "확실히 어울리지 않을 때만" in prompt.user
    assert f'<data label="{GENRES}">\n발라드, 인디\n</data>' in prompt.user
    assert f'<data label="{TRACK}">\n곡: [MV] 밤편지 — 아이유\n</data>' in prompt.user
    bare = build_prompt(PROFILE, [], None, "밤편지", "").user
    assert f'label="{GENRES}"' not in bare and "곡: 밤편지\n" in bare and "기준은 아래 스테이션 컨셉이다" in bare


@pytest.mark.parametrize(("text", "expected"), [
    ('{"fit": true, "known": true, "reason": ""}', Fit(True, True)),
    (REJECT, Fit(False, True, "시끄러운 메탈 곡이라 잔잔한 방송과 맞지 않습니다.")),
    ('판정입니다. {"fit": false, "known": false, "reason": "잘 모르겠습니다"}', Fit(True, True)),  # 모르는 곡은 통과
    ('{"fit": false}', Fit(False, True)),
])
def test_parse_rejects_only_known_songs(text, expected):
    assert parse_fit(text) == expected


@pytest.mark.parametrize("bad", ["모르겠어요", '{"fit": "no"}', '{"known": true}', "[1, 2]"])
def test_parse_fails_on_malformed_output(bad):
    with pytest.raises(ValueError):
        parse_fit(bad)


# ── 판정 ──────────────────────────────────────────────────────────────

def test_judge_rejects_what_the_llm_rejects_and_remembers_the_answer():
    llm = Scripted(REJECT, '{"fit": true, "known": true}')
    judge = _judge(llm)

    async def scenario():
        return (await judge.judge("Master of Puppets", "Metallica"),
                await judge.judge(" master of puppets ", "METALLICA"),  # 같은 곡 — 다시 묻지 않는다
                await judge.judge("밤편지", "아이유"))

    first, again, other = asyncio.run(scenario())
    assert first == again and not first.fit and first.reason.startswith("시끄러운")
    assert other == Fit(True, True) and len(llm.prompts) == 2


def test_judge_passes_when_it_cannot_judge():
    """맞는 곡을 거절당하는 쪽이 더 나쁘다 — 제공자 장애와 해석 실패는 통과로 답하고, 다음에 다시 판정한다."""
    llm = Scripted(LlmError("openai 503"), "곡이 좋네요", REJECT)
    judge = _judge(llm)

    async def scenario():
        return [await judge.judge("Master of Puppets", "Metallica") for _ in range(3)]

    down, garbled, judged = asyncio.run(scenario())
    assert down == garbled == PASS_UNJUDGED and down.payload() == {"fit": True, "judged": False, "reason": None}
    assert not judged.fit, "판정하지 못한 답은 기억하지 않는다"


def test_unsafe_song_info_is_not_sent_to_the_llm():
    llm = Scripted()
    judge = _judge(llm, violates=lambda text: "url" if "http" in text else None)
    assert asyncio.run(judge.judge("들어보세요 http://example.com", "")) == PASS_UNJUDGED
    assert llm.prompts == []


def test_new_persona_judges_with_its_own_taste():
    llm = Scripted(REJECT, '{"fit": true, "known": true}')
    judge = _judge(llm)

    async def scenario():
        before = await judge.judge("Master of Puppets", "Metallica")
        judge.use_persona(StationProfile(dj_name="번개", tone="반말", concept="달리는 록 방송"), ["록", "메탈"])
        return before, await judge.judge("Master of Puppets", "Metallica")

    before, after = asyncio.run(scenario())
    assert not before.fit and after.fit
    assert "록, 메탈" in llm.prompts[1].user and "달리는 록 방송" in llm.prompts[1].user


def test_dummy_llm_never_blocks_a_request():
    assert asyncio.run(_judge(DummyLlmClient(delay_sec=0)).judge("아무 곡", "아무 가수")) == Fit(True, True)


# ── REST ──────────────────────────────────────────────────────────────

class NullTransport:
    async def publish_segment(self, sub):
        pass

    async def notify_request_state(self, request_id, state, detail=None):
        pass

    async def notify_station(self, event_type, payload):
        pass


def _events(path) -> list[str]:
    db = sqlite3.connect(path)
    try:
        return [row[0] for row in db.execute("SELECT summary FROM event_log ORDER BY rowid")]
    finally:
        db.close()


def _client(tmp_path, llm, **settings):
    cfg = EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "m.sqlite",
                         safety_rules_path=RULES, **settings)
    manager = EngineManager(cfg)
    engine = StationEngine(StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=30,
                                         topic="수능 응원", music_taste=["발라드"]), cfg, NullTransport())
    engine.song_fit._llm = llm
    # 시험용 클라이언트는 요청을 다른 스레드에서 처리한다 — 엔진의 계측 연결은 만든 스레드에서만 쓸 수 있다.
    # 실제로는 REST와 엔진이 같은 이벤트 루프에서 돈다. 계측 기록은 아래에서 따로 본다
    engine.song_fit._telemetry = None
    manager._stations["st_test"] = engine
    drafter = PersonaDrafter(lambda: DummyLlmClient(delay_sec=0), SafetyChecker(RULES))
    return TestClient(create_app(manager, drafter, cfg, TOKEN)), engine


def test_api_answers_whether_the_song_fits(tmp_path):
    client, engine = _client(tmp_path, Scripted(REJECT, '{"fit": true, "known": true}'))
    url = "/v1/stations/st_test/song-requests/check"
    res = client.post(url, json={"title": "Master of Puppets", "artist": "Metallica"}, headers=AUTH)
    assert res.status_code == 200
    assert res.json() == {"fit": False, "judged": True, "reason": "시끄러운 메탈 곡이라 잔잔한 방송과 맞지 않습니다."}
    assert client.post(url, json={"title": "밤편지"}, headers=AUTH).json() == {"fit": True, "judged": True, "reason": None}
    engine.telemetry.close()


def test_judgements_show_up_on_the_engine_panel(tmp_path):
    cfg = EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "m.sqlite", safety_rules_path=RULES)
    engine = StationEngine(StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=30,
                                         music_taste=["발라드"]), cfg, NullTransport())
    engine.song_fit._llm = Scripted(REJECT, '{"fit": true, "known": true}')

    async def scenario():
        await engine.song_fit.judge("Master of Puppets", "Metallica")
        await engine.song_fit.judge("밤편지")

    asyncio.run(scenario())
    engine.telemetry.close()
    events = _events(tmp_path / "m.sqlite")
    assert any(e.startswith("신청곡 판정 · Master of Puppets — Metallica · 거절") for e in events)
    assert "신청곡 판정 · 밤편지 · 통과" in events


def test_api_needs_a_token_a_running_station_and_a_title(tmp_path):
    client, engine = _client(tmp_path, Scripted())
    url = "/v1/stations/st_test/song-requests/check"
    assert client.post(url, json={"title": "밤편지"}).status_code == 401
    assert client.post("/v1/stations/st_gone/song-requests/check", json={"title": "밤편지"},
                       headers=AUTH).status_code == 404
    assert client.post(url, json={"title": ""}, headers=AUTH).status_code == 422
    assert client.post(url, json={"artist": "아이유"}, headers=AUTH).status_code == 422
    engine.telemetry.close()


def test_api_passes_the_song_when_the_judge_is_too_slow(tmp_path):
    client, engine = _client(tmp_path, Scripted(REJECT, delay=0.5), song_fit_timeout_sec=0.05)
    res = client.post("/v1/stations/st_test/song-requests/check", json={"title": "Master of Puppets"}, headers=AUTH)
    assert res.status_code == 200 and res.json() == {"fit": True, "judged": False, "reason": None}
    engine.telemetry.close()
