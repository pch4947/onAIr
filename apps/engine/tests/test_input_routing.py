"""입력 라우팅 통합 — 문자 낭독, 게시판 사연, 신청곡 한마디, 취소, 코너 알림 (CORNER_DESIGN 4.4, 5.3, 5.4절)."""
import asyncio
import sqlite3
import time
from pathlib import Path

from onair_engine.domain import (
    ListenerRequest,
    RequestState,
    Script,
    StationConfig,
    StationProfile,
    wire_state,
)
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.inputs.inbox import MessageInbox
from onair_engine.inputs.router import normalize_kind
from onair_engine.inputs.stories import MAX_STORIES, StoryPool
from onair_engine.manager import EngineManager
from onair_engine.pipeline.llm import DummyLlmClient, detect_step
from onair_engine.telemetry import Telemetry

ROOT = Path(__file__).resolve().parent.parent
PROFILE = StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송")
# 계약 v1 백엔드의 RequestState — 이 밖의 값을 보내면 이벤트째 :dead로 격리된다
BACKEND_STATES = {"screened", "queued", "generating", "generated", "rejected", "expired"}


class Capture:
    def __init__(self):
        self.segments = []
        self.states = []
        self.events = []

    async def publish_segment(self, sub):
        self.segments.append(sub)

    async def notify_request_state(self, request_id, state):
        self.states.append((request_id, str(state)))

    async def notify_station(self, event_type, payload):
        self.events.append((event_type, payload))


class ReadingLlm:
    """프롬프트의 데이터 블록을 그대로 대본에 넣는다 — 낭독한 것으로 판정되게."""

    def __init__(self):
        self.prompts = []
        self._dummy = DummyLlmClient(delay_sec=0)

    async def generate(self, prompt):
        self.prompts.append(prompt)
        if detect_step(prompt.user) in ("text_read", "story"):
            return Script(text=prompt.user)
        return await self._dummy.generate(prompt)


class FixedLlm:
    """어떤 단계든 같은 대본 — 문자가 대본에 들어가지 않는다."""

    async def generate(self, prompt):
        return Script(text="오늘도 함께해 주셔서 감사합니다.")


def _engine(tmp_path, *, emit_corner_changed=False, **config):
    config.setdefault("format", "talk")
    cfg = StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=30, **config)
    settings = EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "metrics.sqlite",
                              safety_rules_path=ROOT / "config" / "safety_rules.yaml",
                              chat_rules_path=ROOT / "config" / "chat_rules.yaml",
                              formats_dir=ROOT / "config" / "formats", target_buffer_sec=1e9,
                              emit_corner_changed=emit_corner_changed)
    transport = Capture()
    engine = StationEngine(cfg, settings, transport)
    engine.scheduler.pipeline.llm = ReadingLlm()
    return engine, transport


def _states(transport, request_id):
    return [state for rid, state in transport.states if rid == request_id]


def _rows(tmp_path, sql):
    db = sqlite3.connect(tmp_path / "metrics.sqlite")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def _req(request_id, body="내용입니다", who="a", kind="message", at=1.0, nickname=None):
    return ListenerRequest(request_id=request_id, kind=kind, body=body, requester_ref=who,
                           received_at=at, nickname=nickname)


# ── 라우터 ───────────────────────────────────────────────────────────

def test_v1_kinds_are_chat_and_v2_kinds_are_kept():
    assert [normalize_kind(k) for k in ("story", "mood", "chat", "message", "song", "song_request")] == [
        "chat", "chat", "chat", "message", "song_request", "song_request"]


def test_only_states_the_backend_accepts_are_sent():
    assert wire_state(RequestState.INBOXED) == RequestState.QUEUED
    assert wire_state(RequestState.NOT_ON_AIR) == RequestState.EXPIRED
    assert wire_state(RequestState.CANCELLED) is None and wire_state(RequestState.REFLECTED) is None
    for state in RequestState:
        wire = wire_state(state)
        assert wire is None or str(wire) in BACKEND_STATES


def test_news_chat_ends_at_once_as_not_on_air(tmp_path):
    engine, transport = _engine(tmp_path, format="news")
    req = asyncio.run(engine.submit_request("story", "이 뉴스 어떻게 생각하세요", "u1"))
    engine.telemetry.close()
    assert req.state == RequestState.NOT_ON_AIR and engine.scheduler.pending_count == 0
    assert _states(transport, req.request_id) == ["screened", "expired"]
    assert _rows(tmp_path, "SELECT input_kind, route FROM input_log") == [("chat", "not_on_air")]


def test_music_format_does_not_put_messages_on_air(tmp_path):
    engine, _ = _engine(tmp_path, format="music")
    req = asyncio.run(engine.submit_request("message", "문자 보내요", "u1"))
    engine.telemetry.close()
    assert req.state == RequestState.NOT_ON_AIR and len(engine.sources.inbox) == 0


def test_nickname_must_pass_l0_or_is_read_as_anonymous(tmp_path):
    engine, _ = _engine(tmp_path)

    async def scenario():
        ok = await engine.submit_request("message", "문자 보내요", "u1", nickname="밤샘러")
        bad = await engine.submit_request("message", "문자 또 보내요", "u2", nickname="https://x.example")
        return ok, bad

    ok, bad = asyncio.run(scenario())
    engine.telemetry.close()
    assert ok.nickname == "밤샘러" and bad.nickname is None


# ── 문자 받은편지함 ──────────────────────────────────────────────────

def test_inbox_reads_in_arrival_order_one_per_person_when_others_exist():
    inbox = MessageInbox()
    for rid, who in (("a1", "a"), ("a2", "a"), ("b1", "b")):
        inbox.add(_req(rid, body=f"{rid} 내용입니다", who=who))
    picked = [inbox.take(1)[0]["request"].request_id for _ in range(3)]
    assert picked == ["a1", "b1", "a2"]


def test_inbox_still_reads_when_only_one_person_is_writing():
    inbox = MessageInbox()
    inbox.add(_req("a1", body="첫 문자입니다"))
    inbox.add(_req("a2", body="둘째 문자입니다"))
    assert inbox.count() == 2
    assert [inbox.take(1)[0]["request"].request_id for _ in range(2)] == ["a1", "a2"]


def test_inbox_skips_unreadable_too_long_duplicate_and_rule_breaking_messages():
    inbox = MessageInbox(max_chars=10, violates=lambda text: "금칙어" if "금칙어" in text else None)
    for rid, body in (("ok", "읽을 수 있어요"), ("dup", "읽을 수  있어요"), ("long", "가" * 11),
                      ("noise", "ㅋㅋㅋ"), ("bad", "금칙어예요")):
        inbox.add(_req(rid, body=body, who=rid))
    assert inbox.count() == 1
    assert [m["request"].request_id for m in inbox.take(5)] == ["ok"]
    assert inbox.count() == 0, "같은 내용은 다시 읽지 않는다"


def test_inbox_put_back_returns_to_the_front_and_can_be_read_again():
    inbox = MessageInbox()
    first = _req("a1", body="첫 문자입니다")
    inbox.add(first)
    inbox.add(_req("b1", body="둘째 문자입니다", who="b"))
    assert inbox.take(1)[0]["request"] is first
    inbox.put_back(first)
    assert inbox.take(1)[0]["request"] is first


def _messages_engine(tmp_path):
    engine, transport = _engine(tmp_path, corners=["text_corner", "topic_talk"])

    async def scenario():
        one = await engine.submit_request("message", "오늘 시험 끝났어요", "u1", nickname="밤샘러")
        two = await engine.submit_request("message", "저는 내일 면접이에요", "u2")
        await engine.run(max_segments=8)
        return one, two

    return engine, transport, *asyncio.run(scenario())


def test_messages_are_read_with_nicknames_and_reported_as_queued_then_generated(tmp_path):
    engine, transport, one, _ = _messages_engine(tmp_path)
    assert _states(transport, one.request_id) == ["screened", "queued", "generating", "generated"]
    read = [s for s in transport.segments if s.request_ref == one.request_id]
    assert len(read) == 1 and read[0].corner_type == "text_corner"
    prompts = [p.user for p in engine.scheduler.pipeline.llm.prompts]
    assert any("밤샘러" in p and "오늘 시험 끝났어요" in p for p in prompts)
    assert any("익명" in p and "저는 내일 면접이에요" in p for p in prompts)
    assert not any("u1" in p.user + p.system for p in engine.scheduler.pipeline.llm.prompts)


def test_played_message_is_confirmed_and_counted_in_metrics(tmp_path):
    engine, transport, one, _ = _messages_engine(tmp_path)
    engine.scheduler.telemetry = Telemetry(tmp_path / "metrics.sqlite", "st_test")
    segment = next(s for s in transport.segments if s.request_ref == one.request_id)
    engine.scheduler.on_segment_started(segment.id, time.time())
    engine.scheduler.telemetry.close()
    assert one.state == RequestState.REFLECTED
    assert "reflected" not in _states(transport, one.request_id)
    assert _rows(tmp_path, "SELECT chat_type FROM reflection_log") == [("message",)]


def test_message_missing_from_script_is_returned_once_then_rejected(tmp_path):
    engine, transport = _engine(tmp_path, corners=["text_corner", "topic_talk"])
    engine.scheduler.pipeline.llm = FixedLlm()

    async def scenario():
        one = await engine.submit_request("message", "오늘 시험 끝났어요", "u1")
        await engine.submit_request("message", "저는 내일 면접이에요", "u2")
        await engine.run(max_segments=14)
        return one

    one = asyncio.run(scenario())
    states = _states(transport, one.request_id)
    assert states[-1] == "rejected" and states.count("generating") == 2
    assert not any(s.request_ref == one.request_id for s in transport.segments)


def test_leftover_messages_expire_at_broadcast_end(tmp_path):
    engine, transport = _engine(tmp_path)

    async def scenario():
        req = await engine.submit_request("message", "남은 문자예요", "u1")
        stop = asyncio.Event()
        stop.set()
        await engine.scheduler.run(stop)
        return req

    req = asyncio.run(scenario())
    engine.telemetry.close()
    assert _states(transport, req.request_id)[-1] == "expired"


# ── 게시판 사연 ──────────────────────────────────────────────────────

def _story(story_id, body="사연 본문입니다", at=1.0, tags=()):
    return ListenerRequest(request_id=story_id, kind="board_story", body=body, requester_ref="author",
                           received_at=at, tags=list(tags))


def test_story_pool_prefers_topic_match_then_recent():
    pool = StoryPool(topic_words=frozenset({"시험"}))
    pool.add(_story("new", body="어제 여행 다녀왔어요", at=9.0))
    pool.add(_story("body", body="시험 망쳤어요", at=1.0))
    pool.add(_story("tag", body="요즘 힘들어요", at=2.0, tags=["시험"]))
    assert [s["request"].request_id for s in pool.take(3)] == ["tag", "body", "new"]


def test_story_pool_ignores_redelivery_and_caps_candidates():
    pool = StoryPool()
    assert pool.add(_story("s0", at=0.0)) and not pool.add(_story("s0", at=0.0))
    for i in range(1, MAX_STORIES + 1):
        pool.add(_story(f"s{i}", at=float(i)))
    assert len(pool) == MAX_STORIES
    assert not pool.withdraw("s0"), "가장 오래된 사연이 빠졌다"


def _board(story_id="story_1", body="오늘 시험이 끝나서 친구들과 놀러 갔어요", **extra):
    return {"story_id": story_id, "body": body, "author_ref": "user_secret", "posted_at": 1.0,
            "tags": [], **extra}


def test_board_story_is_read_with_story_id_and_no_state_is_sent(tmp_path):
    engine, transport = _engine(tmp_path, corners=["story_corner", "topic_talk"])
    engine.on_board_story(_board(nickname="밤샘러"))
    asyncio.run(engine.run(max_segments=9))
    read = [s for s in transport.segments if s.request_ref == "story_1"]
    assert len(read) == 1 and read[0].corner_type == "story_corner"
    assert _states(transport, "story_1") == [], "게시판 사연은 요청이 아니다"
    prompts = engine.scheduler.pipeline.llm.prompts
    assert any("밤샘러" in p.user for p in prompts)
    assert not any("user_secret" in p.user + p.system for p in prompts)


def test_engine_keeps_its_configured_inbox_and_pool(tmp_path):
    # 빈 inbox·풀은 거짓이다 — `or` 기본값으로 바뀌면 L2 검사와 사연 판정이 빠진다
    engine, _ = _engine(tmp_path)
    engine.telemetry.close()
    assert engine.scheduler.sources.inbox._violates is not None
    assert engine.scheduler.sources.stories.judged
    assert engine.scheduler.sources.stories is engine.story_judge._pool


def test_board_story_failing_l0_is_not_added(tmp_path):
    engine, _ = _engine(tmp_path)
    engine.on_board_story(_board(body="연락 주세요 https://example.com"))
    engine.telemetry.close()
    assert len(engine.sources.stories) == 0


def test_withdrawn_story_is_removed_from_the_pool(tmp_path):
    engine, transport = _engine(tmp_path, corners=["story_corner", "topic_talk"])
    engine.on_board_story(_board())
    engine.on_board_story_withdrawn({"story_id": "story_1"})
    asyncio.run(engine.run(max_segments=9))
    assert not any(s.request_ref == "story_1" for s in transport.segments)


class GatedLlm(ReadingLlm):
    """문자·사연 단계에서 멈춰, 생성 중에 취소·철회를 넣을 수 있게 한다."""

    def __init__(self):
        super().__init__()
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, prompt):
        if detect_step(prompt.user) in ("text_read", "story"):
            self.reached.set()
            await self.release.wait()
        return await super().generate(prompt)


def test_story_withdrawn_during_generation_is_discarded(tmp_path):
    engine, transport = _engine(tmp_path, corners=["story_corner", "topic_talk"])
    engine.on_board_story(_board())

    async def scenario():
        llm = GatedLlm()
        engine.scheduler.pipeline.llm = llm
        task = asyncio.create_task(engine.run(max_segments=9))
        await llm.reached.wait()
        engine.on_board_story_withdrawn({"story_id": "story_1"})
        llm.release.set()
        await task

    asyncio.run(scenario())
    assert not any(s.request_ref == "story_1" for s in transport.segments)
    assert len(engine.sources.stories) == 0, "철회된 사연은 풀로 돌아가지 않는다"
    assert len(transport.segments) >= 2, "뒤 세그먼트는 그대로 나간다"


# ── 신청곡 ───────────────────────────────────────────────────────────

TRACK = {"track_ref": "yt:song", "title": "밤편지", "artist": "아이유", "duration_ms": 200_000}


def test_song_request_comment_reaches_the_intro(tmp_path):
    engine, transport = _engine(tmp_path, format="music")

    async def scenario():
        req = await engine.submit_request("song_request", "시험 끝난 친구에게 보내요", "u1",
                                          request_id="req_song", nickname="밤샘러")
        engine.on_track_queued({"track": TRACK, "source": "request", "request_id": "req_song"})
        await engine.run(max_segments=6)
        return req

    asyncio.run(scenario())
    prompts = [p.user for p in engine.scheduler.pipeline.llm.prompts]
    assert any("한마디: 시험 끝난 친구에게 보내요" in p and "신청자: 밤샘러" in p for p in prompts)
    intro = next(s for s in transport.segments if s.music_ref == "yt:song")
    assert intro.request_ref == "req_song"
    assert _states(transport, "req_song")[-1] == "generated"


def test_track_queued_without_request_arrived_keeps_nickname_only(tmp_path):
    """지금 백엔드는 신청곡을 track.queued로만 보낸다 — 닉네임 소개가 그대로 나가야 한다."""
    engine, transport = _engine(tmp_path, format="music")
    engine.on_track_queued({"track": TRACK, "source": "request", "request_id": "req_x", "nickname": "밤샘러"})
    asyncio.run(engine.run(max_segments=6))
    prompts = [p.user for p in engine.scheduler.pipeline.llm.prompts]
    assert any("신청자: 밤샘러" in p for p in prompts) and not any("한마디" in p for p in prompts)
    intro = next(s for s in transport.segments if s.music_ref == "yt:song")
    assert intro.request_ref is None


def test_song_request_never_played_expires_at_the_end(tmp_path):
    engine, transport = _engine(tmp_path, format="music")

    async def scenario():
        await engine.submit_request("song_request", "", "u1", request_id="req_song")
        stop = asyncio.Event()
        stop.set()
        await engine.scheduler.run(stop)

    asyncio.run(scenario())
    engine.telemetry.close()
    assert _states(transport, "req_song")[-1] == "expired"


# ── 취소 ─────────────────────────────────────────────────────────────

def test_cancel_removes_waiting_inputs_without_sending_a_state(tmp_path):
    engine, transport = _engine(tmp_path)

    async def scenario():
        chat = await engine.submit_request("story", "채팅이에요", "u1")
        message = await engine.submit_request("message", "문자예요", "u2")
        await engine.on_request_cancelled({"request_id": chat.request_id})
        await engine.on_request_cancelled({"request_id": message.request_id})
        await engine.on_request_cancelled({"request_id": "unknown"})
        return chat, message

    chat, message = asyncio.run(scenario())
    engine.telemetry.close()
    assert chat.state == message.state == RequestState.CANCELLED
    assert engine.scheduler.pending_count == 0 and len(engine.sources.inbox) == 0
    assert all(state in BACKEND_STATES for _, state in transport.states)


def test_cancel_during_generation_discards_the_segment(tmp_path):
    engine, transport = _engine(tmp_path, corners=["text_corner", "topic_talk"])

    async def scenario():
        llm = GatedLlm()
        engine.scheduler.pipeline.llm = llm
        one = await engine.submit_request("message", "오늘 시험 끝났어요", "u1")
        await engine.submit_request("message", "저는 내일 면접이에요", "u2")
        task = asyncio.create_task(engine.run(max_segments=9))
        await llm.reached.wait()
        await engine.on_request_cancelled({"request_id": one.request_id})
        llm.release.set()
        await task
        return one

    one = asyncio.run(scenario())
    assert one.state == RequestState.CANCELLED
    assert not any(s.request_ref == one.request_id for s in transport.segments)


# ── 코너 알림 · 송출 이력 ────────────────────────────────────────────

def test_corner_changed_is_off_by_default(tmp_path):
    engine, transport = _engine(tmp_path)
    asyncio.run(engine.run(max_segments=9))
    assert not any(event == "corner.changed" for event, _ in transport.events)


def test_corner_changed_comes_before_the_first_segment_of_each_corner(tmp_path):
    engine, transport = _engine(tmp_path, emit_corner_changed=True, corners=["topic_talk", "story_corner"])
    order = []
    publish, notify = transport.publish_segment, transport.notify_station

    async def record_publish(sub):
        order.append(("segment", sub.id, sub.corner_type))
        await publish(sub)

    async def record_notify(event_type, payload):
        if event_type == "corner.changed":
            order.append(("corner", payload["segment_id"], payload["corner"]))
        await notify(event_type, payload)

    transport.publish_segment, transport.notify_station = record_publish, record_notify
    asyncio.run(engine.run(max_segments=8))
    announced = [e for e in order if e[0] == "corner"]
    assert [e[2] for e in announced[:2]] == ["opening", "topic_talk"]
    for event in announced:
        assert order[order.index(event) + 1] == ("segment", event[1], event[2])
    talk = next(p for name, p in transport.events if name == "corner.changed" and p["corner"] == "topic_talk")
    assert talk["contract"] == {"chat": "steer", "message": "defer", "song_request": "defer"}
    assert talk["title"] == "오늘의 이야기"


def test_published_moves_script_to_aired_log_once(tmp_path):
    engine, transport = _engine(tmp_path)
    asyncio.run(engine.run(max_segments=2))
    engine.scheduler.telemetry = Telemetry(tmp_path / "metrics.sqlite", "st_test")
    segment = transport.segments[0]
    engine.on_segment_published(segment.id, 5.0)
    engine.on_segment_published(segment.id, 6.0)
    engine.on_segment_published("seg_unknown", 7.0)
    engine.scheduler.telemetry.close()
    rows = _rows(tmp_path, "SELECT segment_id, at FROM aired_log")
    assert rows == [(segment.id, 5.0)]


def test_existing_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE input_log (request_id TEXT, station_id TEXT, received_at REAL, chat_type TEXT, "
               "rule TEXT, anchor_id TEXT, anchor_kind TEXT)")
    db.execute("INSERT INTO input_log VALUES ('old', 'st', 1.0, 'reaction', 'pointing', NULL, NULL)")
    db.commit()
    db.close()
    telemetry = Telemetry(path, "st")
    telemetry.log_input(_req("new"), None, "inbox")
    telemetry.close()
    db = sqlite3.connect(path)
    rows = db.execute("SELECT request_id, input_kind, route FROM input_log ORDER BY rowid").fetchall()
    db.close()
    assert rows == [("old", None, None), ("new", "message", "inbox")]


# ── 이벤트 경로 ──────────────────────────────────────────────────────

def test_manager_routes_new_events(tmp_path):
    engine, transport = _engine(tmp_path)
    manager = EngineManager(engine.settings, transport_factory=lambda _: transport, voice_catalog=None)
    arrived = {"type": "request.arrived", "at": time.time(), "payload": {
        "request_id": "req_m", "kind": "message", "body": "문자예요", "requester_ref": "u1", "nickname": "밤샘러"}}
    song = {"type": "request.arrived", "at": time.time(), "payload": {
        "request_id": "req_s", "kind": "song_request", "requester_ref": "u1", "track": TRACK}}

    async def scenario():
        await manager._route_station_event(engine, arrived)
        await manager._route_station_event(engine, song)
        await manager._route_station_event(engine, {"type": "board.story", "payload": _board()})
        await manager._route_station_event(engine, {"type": "board.story", "payload": _board("story_2")})
        await manager._route_station_event(engine, {
            "type": "board.story.withdrawn", "payload": {"story_id": "story_2"}})
        await manager._route_station_event(engine, {
            "type": "request.cancelled", "payload": {"request_id": "req_s"}})

    asyncio.run(scenario())
    engine.telemetry.close()
    assert engine.sources.inbox.items[0].nickname == "밤샘러"
    assert [s.request_id for s in engine.sources.stories.items] == ["story_1"]
    assert "req_s" not in engine.scheduler._song_requests
