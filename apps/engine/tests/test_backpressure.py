"""백엔드 backpressure 반영·오래된 요청 무시·Redis 소켓 타임아웃 (#61)."""
import asyncio
import time
from pathlib import Path

from onair_engine.domain import RequestState, StationConfig, StationProfile
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.manager import EngineManager
from onair_engine.scheduler import scheduler as scheduler_module
from onair_engine.transport import RedisControlChannel, RedisTransport

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"


class Capture:
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


def _settings(tmp_path):
    return EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "metrics.sqlite",
                          safety_rules_path=RULES)


def _config(station_id="st_test"):
    return StationConfig(
        station_id=station_id, broadcast_minutes=30,
        profile=StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송"),
    )


def _engine(tmp_path):
    return StationEngine(_config(), _settings(tmp_path), Capture())


def test_buffer_follows_backend_report(tmp_path):
    scheduler = _engine(tmp_path).scheduler
    scheduler._started_at = time.time()

    assert scheduler._snapshot().buffer_source == "engine"  # 보고 전에는 자체 추정
    scheduler.report_backpressure(120_000, "normal", at=time.time() - 4)
    ctx = scheduler._snapshot()
    assert ctx.buffer_source == "backend"
    assert 115 <= ctx.generated_buffer_sec <= 116.5  # 보고 후 흐른 4초만큼 줄었다
    assert ctx.backpressure is False

    scheduler.report_backpressure(5_000, "low")
    assert scheduler._snapshot().backpressure is True


def test_submissions_after_report_count_toward_buffer(tmp_path):
    scheduler = _engine(tmp_path).scheduler
    scheduler._started_at = time.time()
    scheduler.report_backpressure(10_000, "low")
    scheduler._produced_ms += 7_000  # 보고 이후 엔진이 7초 분량을 더 냈다 — 백엔드는 아직 모른다
    assert 16.5 <= scheduler._snapshot().generated_buffer_sec <= 17.0


def test_stale_report_falls_back_to_engine_estimate(tmp_path):
    scheduler = _engine(tmp_path).scheduler
    scheduler._started_at = time.time()
    scheduler.report_backpressure(
        300_000, "normal", at=time.time() - scheduler_module._BACKEND_REPORT_TTL_SEC - 1)
    ctx = scheduler._snapshot()
    assert ctx.buffer_source == "engine" and ctx.generated_buffer_sec == 0


def test_backend_backlog_stops_generation(tmp_path):
    """백엔드 큐가 목표 버퍼보다 많이 쌓였다고 보고하면 새 멘트를 만들지 않는다 (5분 지연의 원인)."""
    engine = _engine(tmp_path)

    async def scenario(backlog_ms):
        if backlog_ms is not None:
            engine.report_backpressure({"d_total_ms": backlog_ms, "severity": "normal"})
        task = asyncio.create_task(engine.run())
        await asyncio.sleep(1.5)
        engine.stop()
        await task
        return len(engine.transport.segments)

    assert asyncio.run(scenario(300_000)) == 0


def test_without_backlog_generation_continues(tmp_path):
    engine = _engine(tmp_path)

    async def scenario():
        engine.report_backpressure({"d_total_ms": 0, "severity": "critical"})
        task = asyncio.create_task(engine.run())
        await asyncio.sleep(1.5)
        engine.stop()
        await task

    asyncio.run(scenario())
    assert engine.transport.segments  # 큐가 비었다는 보고면 계속 만든다


def test_missing_d_total_keeps_previous_report(tmp_path):
    engine = _engine(tmp_path)
    engine.scheduler._started_at = time.time()
    engine.report_backpressure({"d_total_ms": 60_000, "severity": "normal"})
    engine.report_backpressure({"d_total_ms": None, "severity": "normal"})  # 백엔드가 큐를 못 읽은 주기
    assert engine.scheduler._snapshot().generated_buffer_sec > 55


def test_manager_routes_backpressure_and_drops_stale_requests(tmp_path):
    transport = Capture()
    manager = EngineManager(_settings(tmp_path), transport_factory=lambda sid: transport)

    async def scenario():
        engine = await manager.start_station(_config())
        while transport.inbox is None:
            await asyncio.sleep(0.01)
        now = time.time()
        await transport.inbox.put({"type": "backpressure", "at": now,
                                   "payload": {"d_total_ms": 90_000, "severity": "normal"}})
        await transport.inbox.put({"type": "request.arrived", "at": now - 3600, "payload": {
            "request_id": "req_old", "kind": "story", "body": "어제 보낸 사연"}})
        await transport.inbox.put({"type": "request.arrived", "at": now, "payload": {
            "request_id": "req_new", "kind": "story", "body": "방금 보낸 사연"}})
        for _ in range(200):
            if any(rid == "req_new" for rid, _ in transport.states):
                break
            await asyncio.sleep(0.01)
        source = engine.scheduler._snapshot().buffer_source
        await manager.close_all()
        return source

    source = asyncio.run(scenario())
    assert source == "backend"
    assert ("req_old", RequestState.REJECTED) in transport.states
    assert not any(rid == "req_old" and s != RequestState.REJECTED for rid, s in transport.states)
    assert ("req_new", RequestState.SCREENED) in transport.states


def test_redis_socket_timeout_outlasts_blocking_read():
    # 연결 없이 클라이언트만 만든다 — from_url은 첫 명령 전까지 접속하지 않는다
    transport = RedisTransport("redis://127.0.0.1:6379/0", "st_test", block_ms=5_000)
    control = RedisControlChannel("redis://127.0.0.1:6379/0", block_ms=5_000)
    for client in (transport._redis, control._redis):
        timeout = client.connection_pool.connection_kwargs["socket_timeout"]
        assert timeout > 5.0
