"""편성 관리자 (ENGINE_ARCHITECTURE 3.1·4.1절) — 클락대로 진행, 순서 보장 제출, 과도기 인용, 곡."""
import asyncio
import sqlite3
from pathlib import Path

from onair_engine.domain import (
    RequestState,
    Script,
    SegmentKind,
    SegmentSubmission,
    StationConfig,
    StationProfile,
)
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.pipeline.llm import detect_step

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"
PROFILE = StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송")


class Capture:
    def __init__(self):
        self.segments: list[SegmentSubmission] = []
        self.states = []
        self.station_events = []

    async def publish_segment(self, sub):
        self.segments.append(sub)

    async def notify_request_state(self, request_id, state):
        self.states.append((request_id, state))

    async def notify_station(self, event_type, payload):
        self.station_events.append((event_type, payload))


def _engine(tmp_path, **config) -> tuple[StationEngine, Capture]:
    cfg = StationConfig(station_id="st_test", profile=PROFILE,
                        broadcast_minutes=config.pop("broadcast_minutes", 30), **config)
    settings = EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "metrics.sqlite",
                              safety_rules_path=RULES, target_buffer_sec=1e9)
    transport = Capture()
    return StationEngine(cfg, settings, transport), transport


def _glue(sub: SegmentSubmission) -> bool:
    return "/glue/" in sub.audio_ref


class FakePipeline:
    """단계 이름을 세그먼트 id로 돌려준다. 앞 단계일수록 오래 걸린다 — 끝난 순서가 뒤집힌다."""

    def __init__(self):
        self.calls = 0

    async def run(self, job):
        self.calls += 1
        await asyncio.sleep(0.05 if self.calls == 1 else 0)
        return SegmentSubmission(
            id=job.step, station_id="st_test", audio_ref=f"st_test/{job.step}.wav",
            duration_ms=1000, corner_type=job.corner_type, kind=SegmentKind.SPEECH,
            priority=50, reorderable=True,
            request_ref=job.material.quotes[0].request_id if job.material.quotes else None,
            music_ref=job.material.track_ref,
        )


def test_segments_are_published_in_planned_order(tmp_path):
    engine, transport = _engine(tmp_path)
    engine.scheduler.pipeline = FakePipeline()
    asyncio.run(engine.run(max_segments=6))
    order = ["glue" if _glue(s) else s.id for s in transport.segments]
    # 첫 멘트가 가장 늦게 끝나도 제출은 편성 순서대로, 코너 콜은 앞 코너가 다 나간 뒤에
    assert order == ["opening_greeting", "topic_intro", "corner_preview", "glue", "talk_open",
                     "talk"]


def test_corner_log_records_fallback_for_story_corner(tmp_path):
    engine, _ = _engine(tmp_path, corners=["story_corner", "topic_talk"])
    engine.scheduler.pipeline = FakePipeline()
    asyncio.run(engine.run(max_segments=6))
    db = sqlite3.connect(tmp_path / "metrics.sqlite")
    events = db.execute("SELECT corner_type, event FROM corner_log ORDER BY rowid").fetchall()
    # 게시판 사연(M4) 전에는 사연 코너가 주제 토크로 대체된다
    assert ("story_corner", "fallback") in events
    assert events[:3] == [("opening", "enter"), ("opening", "exit"), ("story_corner", "fallback")]


def test_request_is_quoted_once_and_marked_generated(tmp_path):
    engine, transport = _engine(tmp_path)

    async def scenario():
        req = await engine.submit_request("story", "저는 새벽에 컵라면이요", "u1")
        await engine.run(max_segments=6)
        return req

    req = asyncio.run(scenario())
    quoted = [s for s in transport.segments if s.request_ref == req.request_id]
    assert len(quoted) == 1
    states = [s for rid, s in transport.states if rid == req.request_id]
    assert states[-2:] == [RequestState.GENERATING, RequestState.GENERATED]


class IgnoresQuotes:
    """인용 지시를 무시하는 LLM — 대본에 채팅이 빠진다."""

    async def generate(self, prompt):
        return Script(text=f"{detect_step(prompt.user)} 멘트입니다. 오늘도 고마워요.")


def test_missing_quote_puts_request_back(tmp_path):
    engine, transport = _engine(tmp_path)
    engine.scheduler.pipeline.llm = IgnoresQuotes()

    async def scenario():
        req = await engine.submit_request("story", "저는 새벽에 컵라면이요", "u1")
        await engine.run(max_segments=4)
        return req

    req = asyncio.run(scenario())
    assert not any(s.request_ref for s in transport.segments)
    states = [s for rid, s in transport.states if rid == req.request_id]
    assert states[-1] == RequestState.QUEUED  # 인용 간격(참여 강도)이 지나면 다시 시도한다
    assert engine.scheduler.pending_count == 1


def test_news_format_does_not_quote_requests(tmp_path):
    engine, transport = _engine(tmp_path, format="news")

    async def scenario():
        req = await engine.submit_request("story", "이 뉴스 어떻게 생각하세요", "u1")
        await engine.run(max_segments=4)
        return req

    req = asyncio.run(scenario())
    assert not any(s.request_ref for s in transport.segments)
    assert RequestState.GENERATING not in [s for rid, s in transport.states if rid == req.request_id]


def test_queued_track_is_introduced_and_fills_buffer(tmp_path):
    engine, transport = _engine(tmp_path, format="music")
    engine.on_track_queued({"track": {"track_ref": "yt:abc", "title": "밤편지", "artist": "아이유",
                                      "duration_ms": 200_000}, "source": "playlist"})
    asyncio.run(engine.run(max_segments=4))
    intros = [s for s in transport.segments if s.music_ref]
    assert intros[0].music_ref == "yt:abc"
    # 곡 소개 뒤에는 백엔드가 그 곡을 튼다 — 곡 길이만큼 버퍼가 찬다 (엔진 자체 추정)
    assert engine.scheduler._buffer_sec(engine.scheduler._started_at)[0] > 200


def test_first_song_counts_for_ending_but_not_for_buffer(tmp_path):
    engine, _ = _engine(tmp_path, first_song={"track_ref": "yt:first", "title": "밤편지",
                                              "artist": "아이유", "duration_ms": 240_000})
    s = engine.scheduler
    s._started_at = s._end_at = 0.0  # 계산만 본다
    s._end_at = 1800.0
    assert s._buffer_sec(0.0)[0] == 0  # 오프닝을 곡이 나가는 동안 미리 만든다
    assert s._remaining_sec(0.0) == 1800 - 240


def test_short_broadcast_reaches_closing_and_stops(tmp_path):
    engine, transport = _engine(tmp_path, format="music", broadcast_minutes=1)
    asyncio.run(engine.run())  # max_segments 없이 — 엔딩을 마치면 스스로 끝난다
    corners = [s.corner_type for s in transport.segments]
    assert corners[0] == "opening" and corners[-1] == "closing"
    assert transport.segments[-1].music_ref  # 마지막 곡


def test_glue_prerender_covers_format_corners(tmp_path):
    engine, _ = _engine(tmp_path)
    asyncio.run(engine.glue.prerender())
    ref, ms = engine.glue.get("topic_talk", "call")
    assert ref.startswith("st_test/glue/") and ms > 0
    assert engine.glue.get("opening", "call") is None  # 문구가 없는 코너
