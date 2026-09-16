"""RedisTransport — fakeredis로 계약(docs/ENGINE_REDIS_CONTRACT.md)대로 발행·소비하는지 확인."""
import asyncio
import json
from pathlib import Path

import fakeredis
from redis.exceptions import ConnectionError as RedisConnectionError

from onair_engine.domain import (
    RequestState,
    SegmentKind,
    SegmentSubmission,
    StationConfig,
    StationProfile,
)
from onair_engine.engine import EngineSettings
from onair_engine.manager import EngineManager
from onair_engine.transport import RedisTransport

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"
SEGMENT = SegmentSubmission(
    id="seg_test01", station_id="st_test", audio_ref="st_test/seg_test01.mp3",
    duration_ms=4200, corner_type="filler", kind=SegmentKind.FILLER,
    priority=50, reorderable=True,
)


def _transport(client, consumer="engine-test"):
    return RedisTransport("redis://unused", "st_test", client=client, consumer=consumer,
                          block_ms=10)


def _event(event_type, payload):
    return {
        "type": event_type, "contract_version": "1", "event_id": f"evt_{event_type}",
        "station_id": "st_test", "at": "1789000000.0",
        "payload": json.dumps(payload, ensure_ascii=False),
    }


def test_publish_writes_envelope_to_out_stream():
    async def scenario():
        r = fakeredis.FakeAsyncRedis(decode_responses=True)
        transport = _transport(r)
        await transport.publish_segment(SEGMENT)
        await transport.notify_request_state("req_1", RequestState.SCREENED)
        return await r.xrange("engine:st_test:out")

    (_, segment), (_, state) = asyncio.run(scenario())

    assert segment["type"] == "segment.submitted"
    assert segment["contract_version"] == "1"
    assert segment["station_id"] == "st_test"
    payload = json.loads(segment["payload"])
    assert payload["id"] == "seg_test01"
    assert payload["audio_ref"] == "st_test/seg_test01.mp3"
    assert payload["kind"] == "filler"
    assert state["type"] == "request.state"
    assert json.loads(state["payload"]) == {"request_id": "req_1", "state": "screened"}


def test_events_are_acked_after_handling_and_redelivered_after_restart():
    async def scenario():
        r = fakeredis.FakeAsyncRedis(decode_responses=True)
        transport = _transport(r)
        await transport._ensure_group()
        await r.xadd(transport.in_stream, _event("request.arrived", {"request_id": "req_b1"}))
        await r.xadd(transport.in_stream, {"type": "broken"})  # 계약 위반 — 건너뛰고 ACK
        await r.xadd(transport.in_stream, _event("station.closed", {}))

        events = transport.events()
        first = await anext(events)
        second = await anext(events)  # 여기서 first가 ACK되고, broken은 흘려보내진다
        await events.aclose()  # second는 처리 완료 전 종료 — pending으로 남아야 한다
        pending = await r.xpending(transport.in_stream, RedisTransport.GROUP)

        restarted = _transport(r)  # 같은 consumer 이름으로 재기동
        again = await anext(restarted.events())
        return first, second, pending, again

    first, second, pending, again = asyncio.run(scenario())

    assert first["type"] == "request.arrived"
    assert first["payload"] == {"request_id": "req_b1"}
    assert second["type"] == "station.closed"
    assert pending["pending"] == 1
    assert again["stream_id"] == second["stream_id"]


class _DownRedis:
    async def xadd(self, *args, **kwargs):
        raise RedisConnectionError("redis down")


def test_publish_failure_does_not_raise():
    """Redis가 죽어 있어도 방송은 계속되어야 한다 — 예외가 위로 새지 않는다."""
    asyncio.run(_transport(_DownRedis()).publish_segment(SEGMENT))


class _EventTransport:
    def __init__(self):
        self.segments = []
        self.states = []

    async def publish_segment(self, sub):
        self.segments.append(sub)

    async def notify_request_state(self, request_id, state):
        self.states.append((request_id, state))

    async def events(self):
        yield {"type": "request.arrived", "payload": {
            "request_id": "req_backend1", "kind": "story",
            "body": "요즘 잠이 안 와요", "requester_ref": "listener_1",
        }}
        while True:
            await asyncio.sleep(3600)
            yield {}


def test_request_arrived_event_keeps_backend_request_id(tmp_path):
    transport = _EventTransport()
    settings = EngineSettings(
        audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "metrics.sqlite",
        safety_rules_path=RULES,
    )
    manager = EngineManager(settings, transport_factory=lambda station_id: transport)
    config = StationConfig(
        station_id="st_test", broadcast_minutes=1,
        profile=StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송"),
    )

    asyncio.run(manager.run_local(config, max_segments=12))

    assert ("req_backend1", RequestState.SCREENED) in transport.states
    assert any(s.request_ref == "req_backend1" for s in transport.segments)
