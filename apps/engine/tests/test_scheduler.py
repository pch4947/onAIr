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


def short_talk(engine: StationEngine) -> StationEngine:
    """조용한 방송에서도 이야기를 한 번에 쓰지 않게 한다 — 멘트 하나씩 만드는 흐름을 보는 테스트용 (#262)."""
    scheduler = engine.scheduler
    scheduler.chat_rules = scheduler.chat_rules.model_copy(update={"block_parts": 1})
    return engine


def _engine(tmp_path, dummy_tracks: bool = True, block: bool = False,
            **config) -> tuple[StationEngine, Capture]:
    cfg = StationConfig(station_id="st_test", profile=PROFILE,
                        broadcast_minutes=config.pop("broadcast_minutes", 30), **config)
    settings = EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "metrics.sqlite",
                              safety_rules_path=RULES, target_buffer_sec=1e9,
                              dummy_tracks=dummy_tracks)
    transport = Capture()
    engine = StationEngine(cfg, settings, transport)
    return (engine if block else short_talk(engine)), transport


def _glue(sub: SegmentSubmission) -> bool:
    return "/glue/" in sub.audio_ref


class FakePipeline:
    """단계 이름을 세그먼트 id로 돌려준다. 앞 단계일수록 오래 걸린다 — 끝난 순서가 뒤집힌다."""

    tts_blocked_until = 0.0

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
    assert all(s.kind == SegmentKind.GLUE for s in transport.segments if _glue(s))  # #103
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


class RejectsFirstIntro(FakePipeline):
    """첫 곡 소개 멘트만 L1/L2 REJECT — 악성 채팅을 인용한 경우."""

    rejected = 0

    async def run(self, job):
        if job.step == "music_intro" and not self.rejected:
            self.rejected += 1
            return None
        return await super().run(job)


def test_rejected_intro_puts_track_back(tmp_path):
    # 곡이 사라지면 곡이 시작되지 않아 백엔드도 다음 곡을 보내지 않는다 — 방송 끝까지 음악이 끊긴다 (#216)
    engine, transport = _engine(tmp_path, dummy_tracks=False)
    engine.scheduler.pipeline = pipeline = RejectsFirstIntro()
    engine.on_track_queued({"track": {"track_ref": "yt:abc", "title": "밤편지", "artist": "아이유",
                                      "duration_ms": 200_000}, "source": "playlist"})
    asyncio.run(engine.run(max_segments=12))
    assert pipeline.rejected == 1
    assert [s.music_ref for s in transport.segments if s.music_ref] == ["yt:abc"]


def test_put_back_keeps_newer_queued_track():
    from onair_engine.corners.run import TrackSource

    tracks = TrackSource(dummy=False)
    tracks.queue({"track_ref": "a", "title": "곡 A"})
    taken = tracks.take()
    tracks.queue({"track_ref": "b", "title": "곡 B"})  # 그 사이 백엔드가 새 곡을 알렸다
    tracks.put_back(taken)
    assert tracks.take()["track_ref"] == "b"
    tracks.put_back(taken)
    assert tracks.take()["track_ref"] == "a"


class SlowIntro(FakePipeline):
    """곡 소개만 오래 걸린다 — 다음 코너가 편성된 뒤에 곡 소개(와 곡 길이)가 제출된다."""

    async def run(self, job):
        await asyncio.sleep(1.0 if job.step == "music_intro" else 0.1)
        self.calls += 1
        return await super().run(job)


def test_corner_budget_excludes_song_introduced_before_it(tmp_path):
    # 곡 길이가 다음 코너 몫으로 잡히면 주제 토크가 '꺼내기 + 한 줄'만 하고 끝난다 (#220)
    engine, transport = _engine(tmp_path, dummy_tracks=False, corners=["topic_talk"])
    engine.scheduler.pipeline = SlowIntro()
    engine.sources.talk_points = lambda run: {"source": "topic"}  # 말할 거리는 떨어지지 않는다 — 예산만 본다
    # 동시 생성 2개 — 곡 뒤 코너의 나머지 단계는 곡 소개가 제출된 뒤에 편성된다 (실제 방송처럼)
    engine.scheduler.max_concurrent = 2
    engine.on_track_queued({"track": {"track_ref": "yt:abc", "title": "밤편지", "artist": "아이유",
                                      "duration_ms": 600_000}, "source": "playlist"})
    asyncio.run(engine.run(max_segments=16))
    intro = next(i for i, s in enumerate(transport.segments) if s.music_ref)
    after = [s.id for s in transport.segments[intro + 1:] if not _glue(s)]
    assert after[:5] == ["talk_resume", "talk", "talk", "talk", "talk"], "곡 뒤 주제 토크가 단계를 다 한다"
    db = sqlite3.connect(tmp_path / "metrics.sqlite")
    lengths = db.execute("SELECT produced_ms FROM corner_log WHERE corner_type = 'topic_talk' "
                         "AND event = 'length'").fetchall()
    assert lengths and all(ms < 60_000 for (ms,) in lengths), "코너 길이에 곡이 들어가지 않는다"


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


def test_dummy_track_is_introduced_but_not_sent_or_timed(tmp_path):
    # 백엔드가 track.queued를 보내기 전(#104) — 더미 곡은 실제로 재생되지 않는다.
    # music_ref로 보내지 않고, 방송 시간에도 더하지 않는다 (더하면 엔딩이 일찍 온다)
    engine, transport = _engine(tmp_path, format="music")
    asyncio.run(engine.run(max_segments=5))
    intros = [s for s in transport.segments if s.corner_type == "music_intro"]
    assert intros and all(s.music_ref is None for s in intros)
    s = engine.scheduler
    assert s._produced_ms == sum(seg.duration_ms for seg in transport.segments)


def test_glue_prerender_covers_format_corners(tmp_path):
    engine, _ = _engine(tmp_path)
    asyncio.run(engine.glue.prerender())
    ref, ms = engine.glue.get("topic_talk", "call")
    assert ref.startswith("st_test/glue/") and ms > 0
    assert engine.glue.get("opening", "call") is None  # 문구가 없는 코너


def test_serve_mode_skips_music_without_backend_track(tmp_path):
    # 운영(--serve)에서는 더미 곡을 소개하지 않는다 — 등록된 곡이 없으면 곡 소개 코너를 건너뛴다
    engine, transport = _engine(tmp_path, dummy_tracks=False)
    asyncio.run(engine.run(max_segments=10))
    corners = {s.corner_type for s in transport.segments}
    assert "music_intro" not in corners and "topic_talk" in corners


def test_music_format_waits_for_track_instead_of_ending(tmp_path):
    engine, transport = _engine(tmp_path, dummy_tracks=False, format="music")

    async def scenario():
        task = asyncio.create_task(engine.run())
        for _ in range(200):  # 오프닝까지 나가고 곡을 기다린다
            if len(transport.segments) >= 3:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(3.5)  # 소재 대기(_NO_MATERIAL_SLEEP_SEC) 한 번 이상
        waiting = len(transport.segments)
        assert not task.done(), "곡이 없다고 방송을 끝내면 안 된다"
        db = sqlite3.connect(tmp_path / "metrics.sqlite")
        skips = db.execute("SELECT COUNT(*) FROM corner_log WHERE event='skip'").fetchone()[0]
        db.close()
        # 기다리는 동안(여러 번 다시 시도) 같은 skip을 쌓지 않는다 — music_intro, artist_talk 한 번씩
        assert skips == 2
        engine.on_track_queued({"track": {"track_ref": "yt:abc", "title": "밤편지",
                                          "artist": "아이유", "duration_ms": 200_000},
                                "source": "playlist"})
        for _ in range(300):  # 기다리던 루프가 깨어나 곡 소개를 낸다
            if any(s.music_ref for s in transport.segments):
                break
            await asyncio.sleep(0.02)
        engine.stop()
        await task
        return waiting

    waiting = asyncio.run(scenario())
    assert [s.corner_type for s in transport.segments[:waiting]] == ["opening"] * waiting
    assert any(s.music_ref == "yt:abc" for s in transport.segments)
