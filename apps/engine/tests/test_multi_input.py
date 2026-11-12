"""한 멘트에 입력 여러 건 — 채팅 여러 건 인용, 주 소재와 채팅 함께 싣기 (계약 v2 input_refs, #267)."""
import asyncio
import sqlite3
import time
from pathlib import Path

from onair_engine.corners.steps import (
    MULTI_QUOTE_RULE,
    MULTI_STEER_RULE,
    QUOTE_RULE,
    STEER_RULE,
    TRACK_LAST_RULE,
    WITH_FEATURE_RULE,
    build_prompt,
)
from onair_engine.domain import (
    Anchor,
    GenerationJob,
    ListenerRequest,
    Material,
    Script,
    StationConfig,
    StationProfile,
)
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.format import Mode
from onair_engine.pipeline.llm import DummyLlmClient
from onair_engine.telemetry import Telemetry

ROOT = Path(__file__).resolve().parent.parent
PROFILE = StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송")
SONG = {"track_ref": "yt:first", "title": "밤편지", "artist": "아이유", "duration_ms": 200_000}
TRACK = {"track_ref": "yt:song", "title": "가을 아침", "artist": "아이유", "duration_ms": 200_000}
A, B = "붕어빵은 역시 팥이죠", "저는 오늘 시험 끝났어요"


def _chat(body: str, rid: str) -> ListenerRequest:
    return ListenerRequest(request_id=rid, kind="chat", body=body, requester_ref=rid, received_at=time.time())


class Capture:
    def __init__(self):
        self.segments = []
        self.states = []

    async def publish_segment(self, sub):
        self.segments.append(sub)

    async def notify_request_state(self, request_id, state, detail=None):
        self.states.append((request_id, str(state)))

    async def notify_station(self, event_type, payload):
        pass


def _engine(tmp_path, contract_version: int, **config) -> tuple[StationEngine, Capture]:
    config.setdefault("format", "talk")
    cfg = StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=30, **config)
    settings = EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "metrics.sqlite",
                              safety_rules_path=ROOT / "config" / "safety_rules.yaml",
                              chat_rules_path=ROOT / "config" / "chat_rules.yaml",
                              formats_dir=ROOT / "config" / "formats", target_buffer_sec=1e9,
                              contract_version=contract_version)
    transport = Capture()
    return StationEngine(cfg, settings, transport), transport


async def _opening_done(engine) -> None:
    """오프닝 멘트가 다 정해질 때까지 기다린다 — 오프닝은 채팅을 한 건만 싣는다 (T-009)."""
    while all(s.corner_type == "opening" for s in engine.transport.segments):
        await asyncio.sleep(0.005)


def _two_chats(engine, segments: int = 12):
    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(engine.scheduler.run(stop, max_segments=segments))
        await _opening_done(engine)
        one = await engine.submit_request("chat", A, "u1", request_id="req_a")
        two = await engine.submit_request("chat", B, "u2", request_id="req_b")
        await task
        return one, two

    reqs = asyncio.run(scenario())
    engine.telemetry.close()
    return reqs


def _rows(tmp_path, sql):
    db = sqlite3.connect(tmp_path / "metrics.sqlite")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


# ── 프롬프트 ───────────────────────────────────────────────────────────

def test_single_quote_prompt_is_what_it_was():
    user = build_prompt("talk", Material(topic="겨울 간식", quotes=[_chat(A, "a")]), PROFILE).user
    assert QUOTE_RULE in user and A in user
    assert MULTI_QUOTE_RULE not in user and WITH_FEATURE_RULE not in user


def test_prompt_asks_for_each_chat_separately():
    user = build_prompt("talk", Material(topic="겨울 간식", quotes=[_chat(A, "a"), _chat(B, "b")]), PROFILE).user
    assert MULTI_QUOTE_RULE in user
    assert user.index(A) < user.index(B), "채팅은 한 줄에 한 건씩 고른 순서대로 들어간다"


def test_song_intro_with_several_inputs_ends_with_the_song():
    track = {"track_ref": "yt:1", "title": "가을 아침", "artist": "아이유"}
    two = build_prompt("music_intro", Material(track=track, quotes=[_chat(A, "a"), _chat(B, "b")]), PROFILE).user
    one = build_prompt("music_intro", Material(track=track, quotes=[_chat(A, "a")]), PROFILE).user
    assert TRACK_LAST_RULE in two, "두 건이면 곡을 먼저 소개하고 채팅을 읽는다 — 순서를 정해 준다"
    assert TRACK_LAST_RULE not in one, "한 건일 때의 프롬프트는 그대로다"
    talk = build_prompt("talk", Material(topic="겨울 간식", quotes=[_chat(A, "a"), _chat(B, "b")]), PROFILE).user
    assert TRACK_LAST_RULE not in talk


def test_steering_chat_is_quoted_last():
    m = Material(topic="겨울 간식", quotes=[_chat(A, "a"), _chat(B, "b")], extra={"steer": "question"})
    user = build_prompt("talk", m, PROFILE).user
    assert MULTI_STEER_RULE in user and STEER_RULE not in user
    assert user.index(B) < user.index(A), "화제가 될 첫 인용을 마지막에 둔다"


def test_prompt_keeps_the_feature_in_the_middle():
    m = Material(topic="겨울 간식", items=[{"nickname": "밤샘러", "body": "퇴근길에 붕어빵을 샀어요."}],
                 quotes=[_chat(A, "a")], feature_ref="story_1")
    user = build_prompt("story", m, PROFILE).user
    assert WITH_FEATURE_RULE in user and MULTI_QUOTE_RULE not in user


# ── 파이프라인 ─────────────────────────────────────────────────────────

class OneOfTwoLlm:
    """첫 채팅만 인용한다."""

    async def generate(self, prompt):
        return Script(text=f"{A}라고 하신 분, 저도 팥이 좋아요. 겨울 간식 이야기를 이어 갈게요.")


def test_only_the_chats_in_the_script_are_carried(tmp_path):
    engine, _ = _engine(tmp_path, 2)
    pipeline = engine.scheduler.pipeline
    pipeline.llm = OneOfTwoLlm()
    job = GenerationJob(job_id="job_1", corner_type="topic_talk", step="talk",
                        material=Material(topic="겨울 간식", quotes=[_chat(A, "req_a"), _chat(B, "req_b")]))
    sub = asyncio.run(pipeline.run(job))
    engine.telemetry.close()
    assert sub.input_refs == ["req_a"] and sub.request_ref == "req_a"
    assert _rows(tmp_path, "SELECT request_ref, result FROM quote_log ORDER BY request_ref") == [
        ("req_a", "ok"), ("req_b", "missing")], "인용 검사 기록은 입력마다 남는다"


# ── 방송 ───────────────────────────────────────────────────────────────

def test_high_intensity_quotes_two_chats_in_one_segment_on_v2(tmp_path):
    engine, transport = _engine(tmp_path, 2, intensity="high", first_song=SONG, corners=["topic_talk"])
    one, two = _two_chats(engine)
    both = [s for s in transport.segments if set(s.input_refs) == {"req_a", "req_b"}]
    assert len(both) == 1, [s.input_refs for s in transport.segments]
    assert both[0].reflection_levels.keys() == {"req_a", "req_b"}
    assert [s for rid, s in transport.states if rid == "req_b"][:2] == ["pooled", "selected"]

    engine.scheduler.telemetry = telemetry = Telemetry(tmp_path / "metrics.sqlite", "st_test")

    async def started():
        engine.on_segment_started(both[0].id, 1000.0)
        await asyncio.sleep(0)

    asyncio.run(started())
    telemetry.close()
    assert one.state == two.state == "reflected"
    assert {rid for rid, s in transport.states if s == "reflected"} == {"req_a", "req_b"}
    assert len(_rows(tmp_path, "SELECT request_id FROM reflection_log")) == 2, "반영 기록도 입력마다 남는다"


def test_v1_still_carries_one_chat_per_segment(tmp_path):
    engine, transport = _engine(tmp_path, 1, intensity="high", first_song=SONG, corners=["topic_talk"])
    _two_chats(engine)
    assert all(len(s.input_refs) <= 1 for s in transport.segments), "v1은 멘트에 한 건만 싣는다"
    assert any(s.request_ref in ("req_a", "req_b") for s in transport.segments)


def test_a_missed_chat_goes_back_while_the_other_is_reflected(tmp_path):
    engine, transport = _engine(tmp_path, 2, intensity="high", first_song=SONG, corners=["topic_talk"])
    dummy = DummyLlmClient(delay_sec=0)

    class DropSecond:
        """두 건을 받으면 둘째를 빠뜨린다 — 한 건만 받으면 인용한다."""

        async def generate(self, prompt):
            if A in prompt.user and B in prompt.user:
                return Script(text=f"{A}, 이렇게 보내 주신 분이 있네요. 이야기를 이어 갈게요.")
            return await dummy.generate(prompt)

        async def complete(self, prompt, *, max_tokens):
            return await dummy.complete(prompt, max_tokens=max_tokens)

    engine.scheduler.pipeline.llm = DropSecond()
    engine.scheduler.intensity = engine.scheduler.intensity.model_copy(update={"quote_gap_sec": 0})
    _two_chats(engine, segments=14)
    first = next(s for s in transport.segments if "req_a" in s.input_refs)
    assert first.input_refs == ["req_a"], "빠진 채팅은 싣지 않는다"
    states_b = [s for rid, s in transport.states if rid == "req_b"]
    assert states_b[:3] == ["pooled", "selected", "pooled"], states_b
    assert any(s.input_refs == ["req_b"] for s in transport.segments), "되돌아간 채팅은 다음 멘트에서 인용된다"


def _pick_two(tmp_path, step_kind: str) -> list[str]:
    """참여 강도 high, 계약 v2에서 채팅 두 건이 기다릴 때 이 단계가 몇 건을 고르는가."""
    engine, _ = _engine(tmp_path, 2, intensity="high")
    scheduler = engine.scheduler
    scheduler.enqueue_request(_chat(A, "req_a"))
    scheduler.enqueue_request(_chat(B, "req_b"))
    anchor = Anchor(id="topic:1", kind="topic", keywords=frozenset(), seq=1)
    picked = scheduler._take_quotes(Mode.QUOTE, step_kind, anchor, Material(topic="겨울 간식"))
    engine.telemetry.close()
    return [req.request_id for req in picked]


def test_opening_quotes_one_chat_even_on_v2_high(tmp_path):
    """오프닝은 버퍼 없이 나간다 — 두 건을 실으면 멘트가 길어져 무음이 된다 (T-009)."""
    assert len(_pick_two(tmp_path, "corner_preview")) == 1
    assert len(_pick_two(tmp_path / "talk", "talk")) == 2


def _pick_for_song_request(tmp_path, contract_version: int):
    """신청곡 소개(주 소재가 있는 멘트)를 만들 때 채팅을 함께 고르는가."""
    engine, _ = _engine(tmp_path, contract_version, format="music")
    scheduler = engine.scheduler
    scheduler.enqueue_request(_chat(A, "req_a"))
    material = Material(topic="겨울 간식", track=dict(TRACK), feature_ref="req_song")
    anchor = Anchor(id="track:1", kind="track", keywords=frozenset(), seq=1)
    picked = scheduler._take_quotes(Mode.QUOTE, "music_intro", anchor, material)
    engine.telemetry.close()
    return [req.request_id for req in picked]


def test_song_request_intro_also_quotes_a_chat_on_v2(tmp_path):
    assert _pick_for_song_request(tmp_path, 2) == ["req_a"]


def test_song_request_intro_carries_only_the_request_on_v1(tmp_path):
    assert _pick_for_song_request(tmp_path, 1) == []


def test_feature_and_chat_go_out_together_with_their_levels(tmp_path):
    engine, transport = _engine(tmp_path, 2, format="music")
    song = ListenerRequest(request_id="req_song", kind="song_request", body="시험 끝난 친구에게 보내요",
                           requester_ref="u1", received_at=time.time(), nickname="밤샘러")
    chat = _chat(A, "req_a")
    track = {**TRACK, "request_id": "req_song", "requester_nickname": "밤샘러", "requester_comment": song.body}
    job = GenerationJob(job_id="job_1", corner_type="music_intro", step="music_intro", feature=song,
                        material=Material(topic="겨울 간식", track=track, quotes=[chat], feature_ref="req_song"))

    async def scenario():
        await engine.scheduler._generate(job, engine.scheduler._reserve())

    asyncio.run(scenario())
    engine.telemetry.close()
    intro = next(s for s in transport.segments if s.music_ref == "yt:song")
    assert intro.input_refs == ["req_song", "req_a"], intro.input_refs
    assert intro.reflection_levels == {"req_song": 3, "req_a": 2}, "신청곡은 주 소재(3), 채팅은 인용(2)"
    assert intro.request_ref == "req_song"
    prompt = build_prompt("music_intro", job.material, PROFILE).user
    assert WITH_FEATURE_RULE in prompt and A in prompt and "신청자: 밤샘러" in prompt


def test_discarded_segment_returns_every_input(tmp_path):
    engine, transport = _engine(tmp_path, 2, intensity="high", first_song=SONG, corners=["topic_talk"])

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(engine.scheduler.run(stop, max_segments=12))
        await _opening_done(engine)
        one = await engine.submit_request("chat", A, "u1", request_id="req_a")
        two = await engine.submit_request("chat", B, "u2", request_id="req_b")
        await task
        both = next(s for s in transport.segments if set(s.input_refs) == {"req_a", "req_b"})
        engine.scheduler.telemetry = Telemetry(tmp_path / "metrics.sqlite", "st_test")
        await engine.scheduler.discard_segments([both.id])
        engine.scheduler.telemetry.close()
        return one, two

    one, two = asyncio.run(scenario())
    assert one.state == two.state == "queued", "둘 다 후보 풀로 돌아간다"
    assert {r.request_id for r in engine.scheduler.pool.items} == {"req_a", "req_b"}
