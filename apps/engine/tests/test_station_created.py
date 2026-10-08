"""station.created → 방 기동/거부 (계약 3.3절) — EngineManager와 engine:control 소비."""
import asyncio
import copy
import json
import sqlite3
from pathlib import Path

import fakeredis
import pytest

from onair_engine import engine as engine_module
from onair_engine.engine import EngineSettings
from onair_engine.manager import EngineManager
from onair_engine.transport import RedisControlChannel, RedisTransport

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"
VOICE = "voice-ko-1"
PAYLOAD = {
    "broadcast_minutes": 30,
    "first_song": {"track_ref": "yt:abc123", "title": "밤편지", "artist": "아이유",
                   "duration_ms": 253000},
    "topic": "시험 기간을 버티는 나만의 방법",
    "persona": {
        "persona_id": "psn_7c1e",
        "style": {"formality": "polite", "energy": "low", "humor": "rare"},
        "voice": VOICE,
        "dj_name": "새벽",
        "concept": "심야 스터디 라디오",
        "tone": "차분하고 따뜻한 존댓말",
        "examples": ["새벽 한 시가 넘었네요.", "오늘 분량을 다 못 끝내도 괜찮아요.",
                     "졸리면 물 한 잔이요."],
    },
}


class Capture:
    """스테이션별 가짜 transport — 제출·통보를 모으고, 넣어 준 이벤트를 흘려보낸다."""

    def __init__(self):
        self.segments, self.states, self.station_events = [], [], []
        self.inbox: asyncio.Queue | None = None

    async def publish_segment(self, sub):
        self.segments.append(sub)

    async def notify_request_state(self, request_id, state):
        self.states.append((request_id, state))

    async def notify_station(self, event_type, payload):
        self.station_events.append((event_type, payload))

    async def events(self):
        self.inbox = asyncio.Queue()
        while True:
            yield await self.inbox.get()


def _settings(tmp_path, **kw):
    return EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "metrics.sqlite",
                          safety_rules_path=RULES, **kw)


def _manager(tmp_path, voices=(VOICE,), **kw):
    transports: dict[str, Capture] = {}

    def factory(station_id):
        # 거부 통보용 transport도 같은 방 ID로 만들어지므로 기록을 이어 붙인다
        return transports.setdefault(station_id, Capture())

    manager = EngineManager(_settings(tmp_path, **kw), transport_factory=factory,
                            voice_catalog=lambda: list(voices))
    return manager, transports


def _created(station_id="st_a", payload=PAYLOAD):
    return {"type": "station.created", "station_id": station_id, "payload": payload}


async def _until(cond, timeout=5.0):
    async def poll():
        while not cond():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(poll(), timeout)


def test_station_created_starts_station_with_its_persona(tmp_path):
    manager, transports = _manager(tmp_path)

    async def scenario():
        await manager.handle_event(_created())
        engine = manager.stations["st_a"]
        await _until(lambda: transports["st_a"].segments)
        await manager.close_station("st_a")
        return engine

    engine = asyncio.run(scenario())

    assert transports["st_a"].station_events[0] == (
        "station.started", {"topic": "시험 기간을 버티는 나만의 방법"})
    assert engine.config.voice == VOICE and engine.config.persona_id == "psn_7c1e"
    assert engine.config.profile.dj_name == "새벽"
    assert engine.config.first_song["track_ref"] == "yt:abc123"
    assert "st_a" not in manager.stations  # 방송이 끝나면 정리된다
    db = sqlite3.connect(tmp_path / "metrics.sqlite")
    (event, snapshot), _ = db.execute(
        "SELECT event, payload_json FROM station_log WHERE station_id='st_a' ORDER BY at").fetchall()
    assert event == "station.created" and json.loads(snapshot) == PAYLOAD  # persona 사본


def test_missing_topic_falls_back_to_concept(tmp_path):
    manager, transports = _manager(tmp_path)
    payload = {k: v for k, v in PAYLOAD.items() if k != "topic"}

    async def scenario():
        await manager.handle_event(_created(payload=payload))
        await _until(lambda: transports["st_a"].station_events)
        await manager.close_station("st_a")

    asyncio.run(scenario())
    assert transports["st_a"].station_events[0] == ("station.started", {"topic": "심야 스터디 라디오"})


@pytest.mark.parametrize("mutate, reason, detail", [
    (lambda p: p["persona"].update(examples=["하나"]), "invalid_payload", "persona.examples"),
    (lambda p: p.update(broadcast_minutes=120), "invalid_payload", "broadcast_minutes"),
    (lambda p: p["persona"].pop("persona_id"), "invalid_payload", "persona_id"),
    (lambda p: p["persona"].update(voice="voice-en-9"), "unknown_voice", "voice-en-9"),
])
def test_bad_station_created_is_rejected(tmp_path, mutate, reason, detail):
    manager, transports = _manager(tmp_path)
    payload = copy.deepcopy(PAYLOAD)
    mutate(payload)

    asyncio.run(manager.handle_event(_created(payload=payload)))

    [(event_type, body)] = transports["st_a"].station_events
    assert event_type == "station.rejected"
    assert body["reason"] == reason and detail in body["detail"]
    assert manager.stations == {}


def test_llm_without_key_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    manager, transports = _manager(tmp_path, llm="claude")

    asyncio.run(manager.handle_event(_created()))

    [(event_type, body)] = transports["st_a"].station_events
    assert (event_type, body["reason"]) == ("station.rejected", "llm_unavailable")
    assert "ANTHROPIC_API_KEY" in body["detail"]


def test_ack_prerender_failure_is_rejected(tmp_path, monkeypatch):
    class BrokenTts:
        file_ext = ".mp3"
        cache_namespace = "broken"

        async def synthesize(self, text, out_path):
            raise RuntimeError("cartesia TTS 400: voice not found")

    monkeypatch.setattr(engine_module, "make_tts", lambda kind, voice=None: BrokenTts())
    manager, transports = _manager(tmp_path)

    async def scenario():
        await manager.handle_event(_created())
        await _until(lambda: not manager.stations)

    asyncio.run(scenario())
    [(event_type, body)] = transports["st_a"].station_events
    assert (event_type, body["reason"]) == ("station.rejected", "tts_unavailable")
    assert "voice not found" in body["detail"]


def test_voice_catalog_is_refreshed_once_for_new_voice(tmp_path):
    calls = []

    def catalog():
        calls.append(1)
        return [VOICE] if len(calls) == 1 else [VOICE, "voice-ko-new"]

    manager = EngineManager(_settings(tmp_path), transport_factory=lambda sid: Capture(),
                            voice_catalog=catalog)

    async def scenario():
        known = [await manager._voice_known(VOICE), await manager._voice_known(VOICE),
                 await manager._voice_known("voice-ko-new")]
        return known

    assert asyncio.run(scenario()) == [True, True, True]
    assert len(calls) == 2  # 처음 한 번 + 모르는 보이스가 왔을 때 한 번


def test_duplicate_station_created_is_ignored(tmp_path):
    manager, transports = _manager(tmp_path)

    async def scenario():
        await manager.handle_event(_created())
        first = manager.stations["st_a"]
        await manager.handle_event(_created())  # at-least-once 재전달
        same = manager.stations["st_a"] is first
        await manager.close_station("st_a")
        return same

    assert asyncio.run(scenario())
    assert [e for e, _ in transports["st_a"].station_events].count("station.rejected") == 0


def test_two_stations_run_at_once_and_close_on_event(tmp_path):
    """M3 완료 기준 — 스테이션 2개 이상 동시 방송. station.closed(:in)로 한쪽만 닫는다."""
    manager, transports = _manager(tmp_path)

    async def scenario():
        await manager.handle_event(_created("st_a"))
        await manager.handle_event(_created("st_b"))
        await _until(lambda: transports["st_a"].segments and transports["st_b"].segments)
        both = set(manager.stations)
        await _until(lambda: transports["st_a"].inbox is not None)
        await transports["st_a"].inbox.put({"type": "station.closed", "payload": {}})
        await _until(lambda: "st_a" not in manager.stations)
        remaining = set(manager.stations)
        await manager.close_all()
        return both, remaining

    both, remaining = asyncio.run(scenario())
    assert both == {"st_a", "st_b"}
    assert remaining == {"st_b"}
    assert manager.stations == {}


def test_serve_consumes_control_stream_end_to_end(tmp_path):
    """백엔드가 engine:control에 XADD → 엔진이 방을 띄우고 :out에 station.started를 낸다."""

    async def scenario():
        # 실제처럼 연결을 따로 둔다 — 블로킹 XREADGROUP이 한 연결을 같이 쓰면 서로 막힌다
        server = fakeredis.FakeServer()

        def client():
            return fakeredis.FakeAsyncRedis(server=server, decode_responses=True)

        r = client()
        manager = EngineManager(
            _settings(tmp_path),
            transport_factory=lambda sid: RedisTransport("redis://unused", sid, client=client(),
                                                         consumer="engine-test", block_ms=10),
            voice_catalog=lambda: [VOICE],
        )
        await r.xadd("engine:control", {
            "type": "station.created", "contract_version": "1", "event_id": "evt_demo",
            "station_id": "st_redis", "at": "1789500000",
            "payload": json.dumps(PAYLOAD, ensure_ascii=False),
        })
        control = RedisControlChannel("redis://unused", client=client(), consumer="engine-test",
                                      block_ms=10)
        serving = asyncio.create_task(manager.serve(control))

        async def started():
            return [f for _, f in await r.xrange("engine:st_redis:out")
                    if f["type"] == "station.started"]

        found = []
        for _ in range(250):
            found = await started()
            if found:
                break
            await asyncio.sleep(0.02)
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serving
        pending = await r.xpending("engine:control", "engine")
        return found, pending, manager

    found, pending, manager = asyncio.run(scenario())
    assert json.loads(found[0]["payload"]) == {"topic": "시험 기간을 버티는 나만의 방법"}
    assert found[0]["station_id"] == "st_redis"
    assert pending["pending"] == 0  # 처리한 station.created는 ACK됐다
    assert manager.stations == {}  # serve 종료 시 방을 모두 닫는다
