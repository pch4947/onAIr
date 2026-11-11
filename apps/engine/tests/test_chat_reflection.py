"""채팅 반영 통합 — 앵커 기록, 만료 통보, 재생 확정과 지표 (CORNER_DESIGN 5.1.3, 9.5절)."""
import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from onair_engine.domain import ChatType, RequestState, StationConfig, StationProfile
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.manager import EngineManager, config_from_created
from onair_engine.pipeline.pipeline import quote_match, quoted
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


def test_expired_input_is_logged_and_sent_to_backend(tmp_path):
    engine, transport = _engine(tmp_path)

    async def scenario():
        req = await engine.submit_request("story", "이 노래 좋아요", "u1")
        await engine.scheduler._expire_inputs(time.time() + 61)
        return req

    req = asyncio.run(scenario())
    engine.telemetry.close()
    assert req.state == RequestState.EXPIRED and engine.scheduler.pending_count == 0
    sent = [state for rid, state in transport.states if rid == req.request_id]
    assert sent.count(RequestState.EXPIRED) == 1 and sent[-1] == RequestState.EXPIRED
    states = [s for (s,) in _rows(tmp_path, "SELECT state FROM request_log")]
    assert states[-1] == "expired"


def test_broadcast_end_expires_leftovers(tmp_path):
    engine, transport = _engine(tmp_path)

    async def scenario():
        req = await engine.submit_request("story", "저는 오늘 쉬는 날이에요", "u1")
        stop = asyncio.Event()
        stop.set()  # 바로 종료
        await engine.scheduler.run(stop)
        return req

    req = asyncio.run(scenario())
    engine.telemetry.close()
    assert req.state == RequestState.EXPIRED
    assert transport.states[-1] == (req.request_id, RequestState.EXPIRED)


def _quoted_run(tmp_path, body="저는 새벽에 컵라면이요", **config):
    engine, transport = _engine(tmp_path, **config)

    async def scenario():
        req = await engine.submit_request("story", body, "u1")
        await engine.run(max_segments=6)
        return req

    req = asyncio.run(scenario())
    segment = next(s for s in transport.segments if s.request_ref == req.request_id)
    return engine, req, segment


def test_quote_check_keeps_chat_and_script(tmp_path):
    # 인용 검사마다 채팅·대본·일치 비율을 남긴다 — 누락의 원인을 대본으로 가르기 위해서다 (#258)
    engine, req, _ = _quoted_run(tmp_path)
    engine.telemetry.close()
    rows = _rows(tmp_path, "SELECT request_ref, kind, result, match_ratio, chat, script, step FROM quote_log")
    _, kind, result, match, chat, script, step = next(r for r in rows if r[0] == req.request_id)
    assert (kind, result, match, chat) == ("chat", "ok", 1.0, req.body)
    assert req.body in script and step


def test_quote_match_is_share_of_chat_bigrams():
    assert quote_match("붕어빵은 팥이죠", "붕어빵은 팥이라고 하신 분이 있네요") == pytest.approx(5 / 6)
    assert quote_match("저도 퇴근 중이에요", "퇴근 중이시라는 분이 있네요") < 0.5, "끝말이 바뀌면 짧은 채팅은 기준에 못 미친다"
    assert quote_match("!!", "아무 말") == 1.0 and quoted("!!", "아무 말"), "묶을 글자가 없으면 인용된 것으로 본다"
    assert not quoted("ㅋㅋㅋ", "웃음이 나네요"), "자음만 있는 채팅은 대본에 그대로 나와야 통과한다"


def test_attributed_paraphrase_counts_as_quote():
    # 2026-11-12 평가의 실제 대본들 — 누락 58건 중 44건이 청취자의 말이라고 밝히며 풀어 말한 것이었다 (#258)
    assert quoted("저는 오늘 김치찌개 끓였어요", "김치찌개를 끓였다는 분이 있네요. 든든한 저녁이었겠어요.")
    assert quoted("DJ님 요리 잘하세요?", "요리 잘하냐고 물어보신 분이 있네요. 잘한다고 하긴 어렵지만요.")
    assert quoted("배고파요", "배고프다고 하신 분이 있네요. 일단 냉장고부터 열어보자.")
    # DJ를 부르는 말은 되읽지 않는다 — 풀어서 한 인용을 볼 때는 빼고 센다
    assert quoted("DJ님은 오늘 저녁 뭐 드셨어요?", "저녁 뭐 먹었냐고 물어본 분이 있네. 나는 아직 못 정했어.")


def test_answer_without_attribution_is_not_a_quote():
    # 답만 하면 청취자는 자기 채팅인 줄 알 수 없다 — 되돌려서 다시 인용하게 한다
    assert not quoted("DJ님은 매운 거 잘 드세요?", "매운 음식은 잘 못 먹지만, 얼큰한 국물은 가끔 생각나요.")
    assert not quoted("DJ님은 오늘 저녁 뭐 드셨어요?", "저는 오늘 저녁에 달걀찜을 먹었어요. 부드러운 한 숟갈이 좋더라고요.")
    # 청취자를 말해도 채팅의 낱말이 남지 않았으면 인용이 아니다
    assert not quoted("배고파요", "벌써 출출해졌다고 하신 분이 있네요.")
    assert not quoted("붕어빵은 팥이죠", "오늘도 많은 분이 계시네요. 겨울밤 이야기를 이어가 볼게요.")


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
