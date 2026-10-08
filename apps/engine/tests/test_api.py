"""엔진 REST (계약 6장) — persona 초안·검증, 상태, 결정 로그 (#64)."""
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from onair_schema import Style

from onair_engine.api import create_app
from onair_engine.engine import EngineSettings
from onair_engine.manager import EngineManager
from onair_engine.personas import PersonaDrafter, base_tone
from onair_engine.pipeline.llm import DummyLlmClient, LlmError
from onair_engine.pipeline.safety import SafetyChecker
from onair_engine.telemetry import Telemetry

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"
TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
VOICE = "voice-ko-1"
FORM = {"formality": "polite", "energy": "low", "humor": "rare",
        "music_taste": ["발라드"], "voice": VOICE, "dj_name": None,
        "host_note": "공부하는 사람 옆에 조용히 있어 주는 DJ"}


class ScriptedLlm:
    """정해 둔 응답을 차례로 돌려주는 LLM — 재생성 경로 확인용."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    async def complete(self, prompt, *, max_tokens):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _draft(name, examples=None):
    return {"dj_name": name, "concept": "심야 스터디 라디오", "tone_detail": "문장을 짧게 끊는다.",
            "examples": examples or ["오늘도 오셨네요.", "잠깐 쉬어 갈게요.", "물 한 잔 드세요."],
            "signature_phrases": ["천천히 가요"], "forbidden": ["다그치기"]}


def _client(tmp_path, llm=None, *, voices=(VOICE,), token=TOKEN):
    settings = EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "m.sqlite",
                              safety_rules_path=RULES)
    manager = EngineManager(settings, voice_catalog=(lambda: list(voices)) if voices else None)
    drafter = PersonaDrafter(lambda: llm or DummyLlmClient(delay_sec=0),
                             SafetyChecker(RULES))
    return TestClient(create_app(manager, drafter, settings, token)), settings


def test_health_is_open_but_v1_needs_token(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/health").json() == {"ok": True, "stations": 0}
    assert client.get("/v1/engine/status").status_code == 401
    bad = {"Authorization": "Bearer wrong"}
    assert client.post("/v1/personas/check", json={"persona": {}}, headers=bad).status_code == 401
    assert client.get("/v1/engine/status", headers=AUTH).status_code == 200


def test_dummy_drafts_are_valid_personas(tmp_path):
    client, _ = _client(tmp_path)
    res = client.post("/v1/personas/drafts", json={"form": FORM, "count": 3}, headers=AUTH)

    assert res.status_code == 200
    drafts = res.json()["drafts"]
    assert len(drafts) == 3
    assert len({d["dj_name"] for d in drafts}) == 3  # 이름이 겹치지 않는다
    tone = base_tone(Style(formality="polite", energy="low", humor="rare"))
    for d in drafts:
        assert "persona_id" not in d
        assert d["style"] == {"formality": "polite", "energy": "low", "humor": "rare"}
        assert d["voice"] == VOICE and d["music_taste"] == ["발라드"]
        assert d["tone"].startswith(tone)  # 기본 말투는 규칙으로 정해진다


def test_fixed_name_and_casual_form(tmp_path):
    client, _ = _client(tmp_path)
    form = {**FORM, "formality": "casual", "dj_name": "도도"}
    drafts = client.post("/v1/personas/drafts", json={"form": form, "count": 2},
                         headers=AUTH).json()["drafts"]
    assert [d["dj_name"] for d in drafts] == ["도도", "도도"]
    assert all("반말" in d["tone"] for d in drafts)


def test_prompt_keeps_host_note_in_data_block(tmp_path):
    llm = ScriptedLlm([json.dumps({"drafts": [_draft("새벽")]}, ensure_ascii=False)])
    client, _ = _client(tmp_path, llm)
    client.post("/v1/personas/drafts", json={"form": FORM, "count": 1}, headers=AUTH)
    user = llm.prompts[0].user
    assert '<data label="호스트 메모">' in user and FORM["host_note"] in user
    assert "초안 개수: 1" in user


@pytest.mark.parametrize("form, field", [
    ({**FORM, "host_note": "연락은 010-1234-5678로 주세요"}, "form.host_note"),  # L0
    ({**FORM, "voice": "voice-en-9"}, "form.voice"),  # 보이스 목록에 없음
])
def test_bad_form_is_422_with_field(tmp_path, form, field):
    client, _ = _client(tmp_path)
    res = client.post("/v1/personas/drafts", json={"form": form}, headers=AUTH)
    assert res.status_code == 422
    assert res.json()["errors"][0]["field"] == field


def test_schema_invalid_form_is_422(tmp_path):
    client, _ = _client(tmp_path)
    res = client.post("/v1/personas/drafts", json={"form": {**FORM, "energy": "max"}},
                      headers=AUTH)
    assert res.status_code == 422


def test_only_failed_drafts_are_regenerated(tmp_path):
    casual = ["오늘도 왔구나.", "잠깐 쉬어 가자.", "물 한 잔 마셔."]  # 존댓말 폼인데 반말 — 탈락
    llm = ScriptedLlm([
        json.dumps({"drafts": [_draft("새벽"), _draft("윤슬", casual)]}, ensure_ascii=False),
        "```json\n" + json.dumps({"drafts": [_draft("다온")]}, ensure_ascii=False) + "\n```",
    ])
    client, _ = _client(tmp_path, llm)
    res = client.post("/v1/personas/drafts", json={"form": FORM, "count": 2}, headers=AUTH)

    assert res.status_code == 200
    assert [d["dj_name"] for d in res.json()["drafts"]] == ["새벽", "다온"]
    assert "초안 개수: 1" in llm.prompts[1].user  # 모자란 개수만 다시 요청
    assert "피할 이름: 새벽" in llm.prompts[1].user


def test_no_usable_draft_is_503(tmp_path):
    llm = ScriptedLlm(["설명만 하고 JSON은 없음"] * 3)
    client, _ = _client(tmp_path, llm)
    res = client.post("/v1/personas/drafts", json={"form": FORM}, headers=AUTH)
    assert res.status_code == 503 and "초안" in res.json()["detail"]
    assert len(llm.prompts) == 3  # 첫 시도 + 재생성 2회


def test_llm_failure_is_503(tmp_path):
    client, _ = _client(tmp_path, ScriptedLlm([LlmError("openai 429: no credits")]))
    res = client.post("/v1/personas/drafts", json={"form": FORM}, headers=AUTH)
    assert res.status_code == 503 and "LLM" in res.json()["detail"]


def test_missing_llm_key_is_503_not_startup_failure(tmp_path):
    settings = EngineSettings(sqlite_path=tmp_path / "m.sqlite", safety_rules_path=RULES)

    def no_key():
        raise RuntimeError("openai LLM을 쓰려면 OPENAI_API_KEY가 필요합니다")

    app = create_app(EngineManager(settings), PersonaDrafter(no_key, SafetyChecker(RULES)),
                     settings, None)  # 토큰 없음 = 인증 생략
    res = TestClient(app).post("/v1/personas/drafts", json={"form": FORM})
    assert res.status_code == 503 and "OPENAI_API_KEY" in res.json()["detail"]


def _persona(**overrides):
    persona = {"style": {"formality": "polite", "energy": "low", "humor": "rare"},
               "voice": VOICE, "dj_name": "새벽", "concept": "심야 스터디 라디오",
               "tone": "차분한 존댓말", "examples": ["하나요.", "둘이요.", "셋이요."]}
    persona.update(overrides)
    return persona


def test_check_accepts_valid_persona(tmp_path):
    client, _ = _client(tmp_path)
    res = client.post("/v1/personas/check", json={"persona": _persona()}, headers=AUTH)
    assert res.status_code == 200 and res.json() == {"ok": True}


@pytest.mark.parametrize("persona, field", [
    (_persona(examples=["하나요."]), "persona.examples"),
    (_persona(forbidden="문자열 하나"), "persona.forbidden"),
    (_persona(examples=["하나요.", "메일은 a@b.com으로요.", "셋이요."]), "persona.examples.1"),
    (_persona(voice="voice-en-9"), "persona.voice"),
])
def test_check_reports_field_errors(tmp_path, persona, field):
    client, _ = _client(tmp_path)
    res = client.post("/v1/personas/check", json={"persona": persona}, headers=AUTH)
    assert res.status_code == 422
    assert field in [e["field"] for e in res.json()["errors"]]


def test_decisions_are_paged_by_cursor(tmp_path):
    client, settings = _client(tmp_path)
    telemetry = Telemetry(settings.sqlite_path, "st_a")
    for i in range(5):
        telemetry._db.execute("INSERT INTO decision_log VALUES (?,?,?,?)",
                              ("st_a", float(i), json.dumps({"i": i}), json.dumps({"action": "wait"})))
    telemetry._db.commit()

    first = client.get("/v1/stations/st_a/decisions?limit=2", headers=AUTH).json()
    assert [d["context"]["i"] for d in first["decisions"]] == [0, 1]
    second = client.get(f"/v1/stations/st_a/decisions?limit=2&after={first['next']}",
                        headers=AUTH).json()
    assert [d["context"]["i"] for d in second["decisions"]] == [2, 3]
    last = client.get(f"/v1/stations/st_a/decisions?limit=2&after={second['next']}",
                      headers=AUTH).json()
    assert [d["context"]["i"] for d in last["decisions"]] == [4] and last["next"] is None
    other = client.get("/v1/stations/st_b/decisions", headers=AUTH).json()
    assert other == {"decisions": [], "next": None}


def test_status_reports_latency_p95(tmp_path):
    client, settings = _client(tmp_path)
    assert client.get("/v1/engine/status", headers=AUTH).json()["recent_latency_p95_ms"] is None
    telemetry = Telemetry(settings.sqlite_path, "st_a")
    now = time.time()
    for i, seconds in enumerate([1.0, 2.0, 3.0, 10.0]):
        for stage, part in (("llm", seconds - 0.5), ("l2", 0.0), ("tts", 0.5)):
            telemetry.log_stage(job_id=f"job{i}", corner_type="filler", request_ref=None,
                                stage=stage, started_at=now - 1, finished_at=now - 1 + part,
                                result="ok")
    body = client.get("/v1/engine/status", headers=AUTH).json()
    assert body["recent_latency_p95_ms"] == 10000
    assert body["station_count"] == 0 and body["max_concurrent_per_station"] == 2


def test_status_counts_running_stations(tmp_path):
    client, settings = _client(tmp_path)
    # 결정 로그 DB는 TestClient와 같은 파일을 본다 — 쓰기 연결이 열려 있어도 읽기 전용 연결로 읽는다
    sqlite3.connect(settings.sqlite_path).close()
    assert client.get("/v1/engine/status", headers=AUTH).json()["stations"] == []


VOICE_LIST = [
    {"id": "v-ko", "name": "Yeji", "gender": "feminine", "description": "", "native": True},
    {"id": "v-en", "name": "Daniel", "gender": "masculine", "description": "", "native": False},
]


def _voice_client(tmp_path, lister):
    settings = EngineSettings(sqlite_path=tmp_path / "m.sqlite", safety_rules_path=RULES,
                              tts="cartesia")
    manager = EngineManager(settings, voice_catalog=lambda: [VOICE])
    drafter = PersonaDrafter(lambda: DummyLlmClient(delay_sec=0), SafetyChecker(RULES))
    return TestClient(create_app(manager, drafter, settings, TOKEN, voice_lister=lister))


def test_voices_are_native_korean_by_default_and_cached(tmp_path):
    calls = []

    def lister():
        calls.append(1)
        return VOICE_LIST

    client = _voice_client(tmp_path, lister)
    native = client.get("/v1/voices", headers=AUTH).json()
    assert native == {"provider": "cartesia", "voices": [VOICE_LIST[0]]}
    every = client.get("/v1/voices?native_only=false", headers=AUTH).json()
    assert [v["name"] for v in every["voices"]] == ["Yeji", "Daniel"]
    assert len(calls) == 1  # 폼을 열 때마다 제공자를 부르지 않는다
    assert client.get("/v1/voices").status_code == 401


def test_voice_list_failure_is_503(tmp_path):
    def lister():
        raise RuntimeError("cartesia TTS를 쓰려면 CARTESIA_API_KEY가 필요합니다")

    res = _voice_client(tmp_path, lister).get("/v1/voices", headers=AUTH)
    assert res.status_code == 503 and "CARTESIA_API_KEY" in res.json()["detail"]


def test_dummy_tts_offers_dev_voices(tmp_path):
    client, _ = _client(tmp_path)  # tts 기본값 dummy
    body = client.get("/v1/voices", headers=AUTH).json()
    assert body["provider"] == "dummy" and len(body["voices"]) == 2
