"""엔진 패널 이벤트 — 방송 중 남는 이벤트와 요약 문구 (계약 6.1절 events)."""
import asyncio
from pathlib import Path

from onair_engine.domain import ListenerRequest, StationConfig, StationProfile
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.inputs.selector import Selection
from onair_engine.sources.rss import Feed, FeedItem, RssCollector
from onair_engine.telemetry import Telemetry, read_events

ROOT = Path(__file__).resolve().parent.parent
PROFILE = StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송")


class Silent:
    async def publish_segment(self, sub):
        pass

    async def notify_request_state(self, request_id, state):
        pass

    async def notify_station(self, event_type, payload):
        pass


def _events(tmp_path):
    return read_events(tmp_path / "m.sqlite", "st_test", limit=500)


def test_broadcast_leaves_panel_events(tmp_path):
    cfg = StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=30, format="talk",
                        topic="겨울 간식", corners=["text_corner", "topic_talk"])
    settings = EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "m.sqlite",
                              safety_rules_path=ROOT / "config" / "safety_rules.yaml",
                              chat_rules_path=ROOT / "config" / "chat_rules.yaml",
                              formats_dir=ROOT / "config" / "formats", target_buffer_sec=1e9)
    engine = StationEngine(cfg, settings, Silent())

    async def scenario():
        await engine.submit_request("message", "오늘 붕어빵 먹었어요", "u1", nickname="밤샘러")
        await engine.submit_request("story", "호떡이 더 좋아요", "u2")
        engine.on_track_queued({"track": {"track_ref": "t1", "title": "첫눈", "artist": "가수"}})
        await engine.run(max_segments=10)

    asyncio.run(scenario())
    events = _events(tmp_path)
    summaries = [e["summary"] for e in events]
    kinds = {e["kind"] for e in events}
    assert {"station", "input", "corner", "generation", "track"} <= kinds
    assert "방송 시작 · 주제 겨울 간식" in summaries
    assert "입력 라우팅 · 문자 → 문자 받은편지함" in summaries
    assert "입력 라우팅 · 채팅 → 채팅 후보" in summaries
    assert "다음 곡 · 첫눈 - 가수" in summaries
    assert "코너 진입 · 오프닝" in summaries and "코너 종료 · 오프닝" in summaries
    assert any(s.startswith("생성 · 오프닝 · LLM ") and s.endswith("초") and "안전 검사 통과 · TTS" in s
               for s in summaries), "세그먼트 하나가 한 줄이다"
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)
    assert not any("u1" in str(e["data"]) for e in events), "내부 ID는 패널에 싣지 않는다"


def test_selection_rss_and_aired_summaries(tmp_path):
    telemetry = Telemetry(tmp_path / "m.sqlite", "st_test")

    picked = ListenerRequest(request_id="r1", kind="chat", body="오늘 너무 피곤해요",
                             requester_ref="u1", received_at=1.0)
    candidates = [{"request_id": f"r{i}"} for i in range(3)]
    telemetry.log_selection(Selection("normal", "relevance", 3, [picked], candidates), None)
    telemetry.log_selection(Selection("quiet", "relevance", 0), None)
    telemetry.log_rss({"IT": 4, "경제": 3}, 1)
    telemetry.log_aired("seg1", 1.0, "가" * 50)
    telemetry.log_track({"track_ref": "t1", "title": "첫눈", "artist": "가수", "request_id": "q1"})
    telemetry.close()
    assert [e["summary"] for e in _events(tmp_path)] == [
        '채팅 선별 · 후보 3건 중 "오늘 너무 피곤해요" 선택 (주제 관련)',
        "기사 수집 · IT 4건, 경제 3건 (피드 1개 실패)",
        "송출 · " + "가" * 40 + "…",
        "신청곡 도착 · 첫눈 - 가수",
    ], "후보가 없는 선별은 남기지 않는다"


def test_generation_stages_are_one_line_per_job(tmp_path):
    telemetry = Telemetry(tmp_path / "m.sqlite", "st_test")
    telemetry.corner_titles = {"topic_talk": "오늘의 이야기", "filler": "대체 멘트"}

    def stage(job, stage, result, sec=0.0, corner="topic_talk"):
        telemetry.log_stage(job_id=job, corner_type=corner, request_ref=None, stage=stage,
                            started_at=10.0, finished_at=10.0 + sec, result=result)

    stage("a", "llm", "ok", 1.24)
    stage("b", "llm", "ok", 0.5)  # 동시 생성 — 섞이지 않아야 한다
    stage("a", "quote", "ok")
    stage("a", "l2", "ok")
    stage("b", "l2", "reject")
    stage("a", "tts", "ok", 0.8)
    stage("c", "llm", "error", 3.0)
    stage("c", "fallback", "filler")
    stage("c", "l2", "ok", corner="filler")
    stage("c", "tts", "ok", 0.3, corner="filler")
    telemetry.close()
    assert [e["summary"] for e in _events(tmp_path)] == [
        "생성 · 오늘의 이야기 · LLM 0.5초 · 안전 검사 차단",
        "생성 · 오늘의 이야기 · LLM 1.2초 · 인용 확인 · 안전 검사 통과 · TTS 0.8초",
        "생성 · 오늘의 이야기 · LLM 실패 · 대체 멘트로 전환",
        "생성 · 대체 멘트 · 안전 검사 통과 · TTS 0.3초",
    ]


def test_rss_refresh_reports_counts_by_section():
    seen = []
    items = [FeedItem(title=f"기사 {i}", summary="", source_url=f"https://example.com/{i}",
                      section="IT" if i < 2 else "경제") for i in range(3)]
    feed = Feed("매체", "http://feed", "IT")
    rss = RssCollector(items, feeds=[feed], fetch=lambda url: (_ for _ in ()).throw(OSError("x")),
                       on_refresh=lambda counts, failed: seen.append((counts, failed)))
    asyncio.run(rss.refresh())
    assert seen == [({"IT": 2, "경제": 1}, 1)]
