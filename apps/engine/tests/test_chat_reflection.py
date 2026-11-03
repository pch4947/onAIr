"""채팅 반영 통합 — 앵커 기록, 만료(백엔드 미통보), 재생 확정과 지표 (CORNER_DESIGN 5.1.3, 9.5절)."""
import asyncio
import sqlite3
import time
from pathlib import Path

from onair_engine.domain import ChatType, RequestState, StationConfig, StationProfile
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.manager import EngineManager, config_from_created
from onair_engine.telemetry import Telemetry, reflection_summary

ROOT = Path(__file__).resolve().parent.parent
PROFILE = StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송")
SONG = {"track_ref": "yt:first", "title": "밤편지", "artist": "아이유", "duration_ms": 200_000}


class Capture:
    def __init__(self):
        self.segments = []
        self.states = []

    async def publish_segment(self, sub):
        self.segments.append(sub)

    async def notify_request_state(self, request_id, state):
        self.states.append((request_id, state))

    async def notify_station(self, event_type, payload):
        pass


def _engine(tmp_path, **config) -> tuple[StationEngine, Capture]:
    cfg = StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=30, **config)
    settings = EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "metrics.sqlite",
                              safety_rules_path=ROOT / "config" / "safety_rules.yaml",
                              chat_rules_path=ROOT / "config" / "chat_rules.yaml",
                              formats_dir=ROOT / "config" / "formats", target_buffer_sec=1e9)
    transport = Capture()
    return StationEngine(cfg, settings, transport), transport


def _rows(tmp_path, sql):
    db = sqlite3.connect(tmp_path / "metrics.sqlite")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def _reopen_telemetry(engine, tmp_path):
    """engine.run이 끝나면 계측을 닫는다 — 그 뒤에 오는 재생 통보를 흉내 내려고 다시 연다."""
    engine.scheduler.telemetry = Telemetry(tmp_path / "metrics.sqlite", "st_test")
    return engine.scheduler.telemetry


def test_chat_during_song_is_anchored_to_it_and_reaches_the_next_talk(tmp_path):
    engine, transport = _engine(tmp_path, first_song=SONG)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(engine.scheduler.run(stop, max_segments=6))
        await asyncio.sleep(0.01)  # 타임라인에 첫 곡이 올라갈 때까지
        req = await engine.submit_request("story", "밤편지 진짜 좋아요", "u1")
        await task
        return req

    req = asyncio.run(scenario())
    engine.telemetry.close()
    assert req.chat_type == ChatType.REACTION and req.rule == "anchor_keyword"
    assert req.anchor_id == "track:1"
    assert any(s.request_ref == req.request_id for s in transport.segments)
    logged = _rows(tmp_path, "SELECT chat_type, rule, anchor_id, anchor_kind FROM input_log")
    assert logged == [("reaction", "anchor_keyword", "track:1", "track")]
    assert _rows(tmp_path, "SELECT situation, criterion FROM selection_log")[0] == ("normal", "relevance")


def test_expired_input_is_logged_but_not_sent_to_backend(tmp_path):
    engine, transport = _engine(tmp_path)

    async def scenario():
        req = await engine.submit_request("story", "이 노래 좋아요", "u1")
        engine.scheduler._expire_inputs(time.time() + 61)
        return req

    req = asyncio.run(scenario())
    engine.telemetry.close()
    assert req.state == RequestState.EXPIRED and engine.scheduler.pending_count == 0
    sent = [state for rid, state in transport.states if rid == req.request_id]
    assert RequestState.EXPIRED not in sent and sent[-1] != RequestState.GENERATING
    states = [s for (s,) in _rows(tmp_path, "SELECT state FROM request_log")]
    assert states[-1] == "expired"


def test_broadcast_end_expires_leftovers(tmp_path):
    engine, _ = _engine(tmp_path, format="news")

    async def scenario():
        req = await engine.submit_request("story", "이 뉴스 어떻게 생각하세요", "u1")
        stop = asyncio.Event()
        stop.set()  # 바로 종료
        await engine.scheduler.run(stop)
        return req

    req = asyncio.run(scenario())
    engine.telemetry.close()
    assert req.state == RequestState.EXPIRED


def _quoted_run(tmp_path, body="저는 새벽에 컵라면이요", **config):
    engine, transport = _engine(tmp_path, **config)

    async def scenario():
        req = await engine.submit_request("story", body, "u1")
        await engine.run(max_segments=6)
        return req

    req = asyncio.run(scenario())
    segment = next(s for s in transport.segments if s.request_ref == req.request_id)
    return engine, req, segment


def test_played_event_confirms_reflection_and_metrics(tmp_path):
    engine, req, segment = _quoted_run(tmp_path)
    telemetry = _reopen_telemetry(engine, tmp_path)

    at = req.received_at + 50.0
    engine.on_segment_played(segment.id, at)
    engine.on_segment_played(segment.id, at + 99.0)  # 재전달 — 처음 값 유지
    telemetry.close()

    rows = _rows(tmp_path, "SELECT request_id, segment_id, chat_type, started_at, quote_age, "
                           "quote_age_net, topic_match, script_chars, quote_chars FROM reflection_log")
    assert len(rows) == 1
    rid, seg, chat_type, started, age, net, match, script_chars, quote_chars = rows[0]
    assert (rid, seg, chat_type) == (req.request_id, segment.id, "standalone")
    assert started == at - segment.duration_ms / 1000
    assert age == net == started - req.received_at
    assert match is None  # 독립형은 맥락 일치를 재지 않는다
    assert script_chars > 0 and quote_chars == len(req.body)
    assert req.state == RequestState.REFLECTED

    summary = reflection_summary(tmp_path / "metrics.sqlite", "st_test")
    assert summary["standalone_reflection_rate"] == 1.0
    assert summary["reaction_reflection_rate"] is None and summary["timely_rate"] is None


def test_reaction_metrics_exclude_time_waiting_for_song(tmp_path):
    engine, transport = _engine(tmp_path, first_song=SONG)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(engine.scheduler.run(stop, max_segments=6))
        await asyncio.sleep(0.01)
        req = await engine.submit_request("story", "밤편지 진짜 좋아요", "u1")
        await task
        return req

    req = asyncio.run(scenario())
    engine.telemetry.close()
    telemetry = _reopen_telemetry(engine, tmp_path)
    segment = next(s for s in transport.segments if s.request_ref == req.request_id)
    song_end = engine.scheduler.timeline.track_end(req.anchor_id)

    engine.on_segment_started(segment.id, song_end + 30.0)
    telemetry.close()

    (age, net, match), = _rows(tmp_path, "SELECT quote_age, quote_age_net, topic_match FROM reflection_log")
    assert age > 150  # 곡이 200초라 도착부터는 오래 기다렸다
    assert abs(net - 30.0) < 1e-6
    assert match == 1
    summary = reflection_summary(tmp_path / "metrics.sqlite", "st_test")
    assert summary["timely_rate"] == 1.0 and summary["topic_match_rate"] == 1.0
    assert reflection_summary(tmp_path / "metrics.sqlite", "st_test", q_max_sec=10)["timely_rate"] == 0.0


def test_manager_routes_playback_events(tmp_path):
    engine, req, segment = _quoted_run(tmp_path)
    telemetry = _reopen_telemetry(engine, tmp_path)
    engine.telemetry = telemetry
    manager = EngineManager(EngineSettings())

    async def scenario():
        for state in ("MIXING", "PUBLISHED"):  # 재생 완료가 아니면 확정하지 않는다
            await manager._route_station_event(engine, {
                "type": "state.transition",
                "payload": {"segment_id": segment.id, "state": state, "at": time.time()}})
        assert req.state != RequestState.REFLECTED
        await manager._route_station_event(engine, {
            "type": "state.transition",
            "payload": {"segment_id": segment.id, "state": "PLAYED", "at": time.time()}})
        await manager._route_station_event(engine, {  # 잘못된 페이로드는 무시한다
            "type": "segment.started", "payload": {"segment_id": 3, "started_at": "x"}})
        await manager._route_station_event(engine, {
            "type": "track.started", "payload": {"track_ref": "yt:none", "started_at": 1.0}})

    asyncio.run(scenario())
    telemetry.close()
    assert req.state == RequestState.REFLECTED


def test_experiment_sets_criterion_and_window(tmp_path):
    engine, _ = _engine(tmp_path, experiment={"selector": "recency", "window_sec": 45})
    engine.telemetry.close()
    assert engine.scheduler.selector.criterion == "recency"
    assert engine.scheduler.pool.reaction_window_sec == 45


def test_station_created_passes_experiment():
    from onair_schema import StationCreated

    payload = {
        "broadcast_minutes": 30,
        "experiment": {"condition": "c1", "selector": "diversity"},
        "persona": {"persona_id": "p", "style": {"formality": "polite", "energy": "low", "humor": "rare"},
                    "voice": "kore", "dj_name": "새벽", "concept": "심야", "tone": "차분",
                    "examples": ["하나", "둘", "셋"]},
    }
    config = config_from_created("st", StationCreated.model_validate(payload), payload)
    assert config.experiment["selector"] == "diversity" and config.experiment["window_sec"] == 60


def test_quote_is_normalized_only_for_speech(tmp_path):
    """분류는 원문으로 하고, TTS 입력만 정규화한다."""
    from onair_engine.audio import CachedTts

    class Recorder:
        file_ext = ".wav"
        cache_namespace = "rec"
        text = None

        async def synthesize(self, text, out_path):
            Recorder.text = text
            Path(out_path).write_bytes(b"x")
            return 100

    asyncio.run(CachedTts(Recorder()).synthesize("좋아요 ㅋㅋㅋㅋ", tmp_path / "a.wav"))
    assert Recorder.text == "좋아요 크크크"
