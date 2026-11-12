"""계약 v2로 보내기 — input_refs·reflection_levels와 새 request.state 값 (ENGINE_REDIS_CONTRACT 10.4·10.5절, #157)."""
import asyncio
import json
from pathlib import Path

import fakeredis

from onair_engine.domain import (
    RequestState,
    SegmentKind,
    SegmentSubmission,
    StationConfig,
    StationProfile,
    segment_payload,
    wire_state,
)
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.main import load_config
from onair_engine.telemetry import Telemetry
from onair_engine.transport import RedisTransport

ROOT = Path(__file__).resolve().parent.parent
PROFILE = StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송")
SONG = {"track_ref": "yt:first", "title": "밤편지", "artist": "아이유", "duration_ms": 200_000}


def _segment(**kw) -> SegmentSubmission:
    return SegmentSubmission(id="seg_1", station_id="st_test", audio_ref="st_test/seg_1.wav", duration_ms=4200,
                             corner_type="topic_talk", kind=SegmentKind.SPEECH, priority=50, reorderable=True, **kw)


class Capture:
    def __init__(self):
        self.segments = []
        self.states = []

    async def publish_segment(self, sub):
        self.segments.append(sub)

    async def notify_request_state(self, request_id, state, detail=None):
        self.states.append((request_id, str(state), detail))

    async def notify_station(self, event_type, payload):
        pass


def _engine(tmp_path, contract_version: int, **config) -> tuple[StationEngine, Capture]:
    cfg = StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=30, **config)
    settings = EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "metrics.sqlite",
                              safety_rules_path=ROOT / "config" / "safety_rules.yaml",
                              chat_rules_path=ROOT / "config" / "chat_rules.yaml",
                              formats_dir=ROOT / "config" / "formats", target_buffer_sec=1e9,
                              contract_version=contract_version)
    transport = Capture()
    return StationEngine(cfg, settings, transport), transport


def _quote_a_chat(engine):
    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(engine.scheduler.run(stop, max_segments=6))
        await asyncio.sleep(0.01)  # 타임라인에 첫 곡이 올라갈 때까지
        req = await engine.submit_request("chat", "밤편지 진짜 좋아요", "u1")
        await task
        return req

    req = asyncio.run(scenario())
    engine.telemetry.close()
    return req


# ── 형식 ───────────────────────────────────────────────────────────────

def test_v1_payload_is_what_it_was():
    payload = segment_payload(_segment(request_ref="req_1", input_refs=["req_1"], reflection_levels={"req_1": 3}))
    assert payload["request_ref"] == "req_1"
    assert "input_refs" not in payload and "reflection_levels" not in payload, "v1 백엔드에는 모르는 필드를 보내지 않는다"


def test_v2_payload_replaces_request_ref_with_input_refs_and_levels():
    payload = segment_payload(_segment(request_ref="req_1", input_refs=["req_1"], reflection_levels={"req_1": 3}), 2)
    assert "request_ref" not in payload
    assert payload["input_refs"] == ["req_1"] and payload["reflection_levels"] == {"req_1": 3}
    empty = segment_payload(_segment(), 2)
    assert empty["input_refs"] == [] and empty["reflection_levels"] == {}, "입력이 없으면 빈 배열과 빈 객체다"


def test_v2_payload_counts_a_bare_request_ref_as_a_quote():
    payload = segment_payload(_segment(request_ref="story_7"), 2)
    assert payload["input_refs"] == ["story_7"] and payload["reflection_levels"] == {"story_7": 2}


def test_v2_request_states():
    assert wire_state(RequestState.QUEUED, 2) == "pooled"
    assert wire_state(RequestState.INBOXED, 2) == "inboxed"
    assert wire_state(RequestState.GENERATING, 2) == "selected"
    assert wire_state(RequestState.REFLECTED, 2) == "reflected"
    assert wire_state(RequestState.NOT_ON_AIR, 2) == "not_on_air"
    assert wire_state(RequestState.CANCELLED, 2) == "cancelled"
    assert wire_state(RequestState.EXPIRED, 2) == "expired" and wire_state(RequestState.REJECTED, 2) == "rejected"
    # v2에 없는 값은 보내지 않는다 — 모르는 값은 백엔드가 이벤트째 격리한다
    assert wire_state(RequestState.SCREENED, 2) is None and wire_state(RequestState.GENERATED, 2) is None
    # v1은 그대로다
    assert wire_state(RequestState.INBOXED) == "queued" and wire_state(RequestState.NOT_ON_AIR) == "expired"
    assert wire_state(RequestState.REFLECTED) is None and wire_state(RequestState.SCREENED) == "screened"


def test_redis_transport_sends_the_configured_version():
    async def scenario():
        r = fakeredis.FakeAsyncRedis(decode_responses=True)
        transport = RedisTransport("redis://unused", "st_test", client=r, consumer="engine-test", block_ms=10,
                                   contract_version=2)
        await transport.publish_segment(_segment(request_ref="req_1", input_refs=["req_1"],
                                                 reflection_levels={"req_1": 2}))
        await transport.notify_request_state("req_1", "reflected", {"segment_id": "seg_1", "started_at": 12.5})
        return await r.xrange("engine:st_test:out")

    (_, segment), (_, state) = asyncio.run(scenario())
    assert segment["contract_version"] == "2" and state["contract_version"] == "2"
    payload = json.loads(segment["payload"])
    assert payload["input_refs"] == ["req_1"] and "request_ref" not in payload
    assert json.loads(state["payload"]) == {"request_id": "req_1", "state": "reflected",
                                            "segment_id": "seg_1", "started_at": 12.5}


def test_contract_version_comes_from_config_and_env(tmp_path, monkeypatch):
    path = tmp_path / "station.yaml"
    path.write_text((ROOT / "config" / "station.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.delenv("ONAIR_CONTRACT_VERSION", raising=False)
    assert load_config(path)[1].contract_version == 1, "기본은 v1이다 — 백엔드가 v2를 받게 된 뒤에 올린다"
    monkeypatch.setenv("ONAIR_CONTRACT_VERSION", "2")
    assert load_config(path)[1].contract_version == 2


# ── 방송 ───────────────────────────────────────────────────────────────

def test_v2_broadcast_reports_pooled_selected_reflected(tmp_path):
    engine, transport = _engine(tmp_path, 2, first_song=SONG)
    req = _quote_a_chat(engine)
    states = [s for rid, s, _ in transport.states if rid == req.request_id]
    assert states[:2] == ["pooled", "selected"], states
    assert not {"screened", "queued", "generating", "generated"} & set(states), "v1 값은 보내지 않는다"
    segment = next(s for s in transport.segments if s.request_ref == req.request_id)
    assert segment.input_refs == [req.request_id]
    assert segment.reflection_levels == {req.request_id: 2}, "인용은 반영 단계 2다"

    engine.scheduler.telemetry = telemetry = Telemetry(tmp_path / "metrics.sqlite", "st_test")

    async def started():
        engine.on_segment_started(segment.id, 1000.0)
        await asyncio.sleep(0)  # 통보 태스크가 돈다

    asyncio.run(started())
    telemetry.close()
    assert transport.states[-1] == (req.request_id, "reflected", {"segment_id": segment.id, "started_at": 1000.0})


def test_v1_broadcast_is_unchanged(tmp_path):
    engine, transport = _engine(tmp_path, 1, first_song=SONG)
    req = _quote_a_chat(engine)
    states = [s for rid, s, _ in transport.states if rid == req.request_id]
    assert states[:3] == ["screened", "generating", "generated"], states
    segment = next(s for s in transport.segments if s.request_ref == req.request_id)

    engine.scheduler.telemetry = telemetry = Telemetry(tmp_path / "metrics.sqlite", "st_test")

    async def started():
        engine.on_segment_started(segment.id, 1000.0)
        await asyncio.sleep(0)

    asyncio.run(started())
    telemetry.close()
    assert "reflected" not in [s for _, s, _ in transport.states], "v1 백엔드는 reflected를 모른다"
