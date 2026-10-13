"""관통 스모크 테스트 — 더미 어댑터로 파이프라인·엔진이 끝까지 도는지 확인."""
import asyncio
from pathlib import Path

from onair_engine.corners import STEP_KINDS, build_prompt
from onair_engine.domain import (
    GenerationJob,
    ListenerRequest,
    Material,
    RequestState,
    SegmentKind,
    StationConfig,
    StationProfile,
)
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.pipeline.llm import DummyLlmClient, detect_step, make_llm
from onair_engine.pipeline.pipeline import GenerationPipeline
from onair_engine.pipeline.safety import SafetyChecker
from onair_engine.pipeline.tts import make_tts
from onair_engine.telemetry import Telemetry

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"
PROFILE = StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송")
TRACK = {"track_ref": "cat_dummy_001", "title": "Midnight Study", "artist": "onAIr Ensemble",
         "duration_ms": 1000, "mood": "calm"}


class CaptureTransport:
    def __init__(self):
        self.segments = []
        self.states = []
        self.station_events = []

    async def publish_segment(self, sub):
        self.segments.append(sub)

    async def notify_request_state(self, request_id, state):
        self.states.append((request_id, state))

    async def notify_station(self, event_type, payload):
        self.station_events.append((event_type, payload))

    async def events(self):
        while True:
            await asyncio.sleep(3600)
            yield {}


def make_pipeline(tmp_path):
    return GenerationPipeline(
        station_id="st_test", profile=PROFILE,
        llm=make_llm("dummy"), tts=make_tts("dummy"), safety=SafetyChecker(RULES),
        telemetry=Telemetry(tmp_path / "metrics.sqlite", "st_test"),
        audio_root=tmp_path / "audio",
    )


def test_dummy_pipeline_produces_segment(tmp_path):
    pipeline = make_pipeline(tmp_path)
    job = GenerationJob(job_id="job_t1", corner_type="filler", step="filler", material=Material())
    sub = asyncio.run(pipeline.run(job))
    assert sub is not None
    assert sub.duration_ms > 0
    assert sub.kind == SegmentKind.FILLER
    # audio_ref는 공유 오디오 루트 기준 POSIX 상대 경로여야 한다 (설계 문서 5.1).
    # 백엔드가 URL로 이어붙이므로 OS 경로 구분자가 섞이면 안 된다.
    assert sub.audio_ref.startswith("st_test/")
    assert "\\" not in sub.audio_ref
    assert (tmp_path / "audio" / sub.audio_ref).exists()


def _material(step: str) -> Material:
    item = {"nickname": "밤샘러", "body": "요즘 잠이 안 와요", "question": "고백은 문자로?",
            "options": ["문자", "전화"]}
    if step == "artist_talk":
        item = TRACK  # 다음 곡을 미리 본다
    elif step.startswith("news_"):
        item = {"title": "도서관 야간 개방 연장", "summary": "시험 기간 운영", "source": "학교 공지"}
    return Material(
        items=[item], track=TRACK, topic="시험 기간 버티기",
        extra={"corners": ["사연 읽는 시간"], "guide": "채팅은 언제든", "covered": ["첫 서리"]},
    )


def test_dummy_llm_writes_readable_script_per_step():
    llm = DummyLlmClient(delay_sec=0, seed=0)
    safety = SafetyChecker(RULES)
    scripts = {}
    for step in STEP_KINDS:
        prompt = build_prompt(step, _material(step), PROFILE)
        assert detect_step(prompt.user) == step, "단계 지시문과 더미 marker 불일치"
        text = asyncio.run(llm.generate(prompt)).text
        assert "{" not in text and safety.check_l2(text) is None, step
        scripts[step] = text

    assert "테스트" in scripts["opening_greeting"]  # DJ 이름
    assert "Midnight Study" in scripts["music_intro"]
    assert "잠이 안 와요" in scripts["story"]
    assert "도서관 야간 개방 연장" in scripts["news_item"]
    assert "시험 기간 버티기" in scripts["topic_intro"]


def test_l0_blocks_contact_info():
    safety = SafetyChecker(RULES)
    assert safety.check_l0("제 번호는 010-1234-5678 이에요") is not None
    assert safety.check_l0("오늘 비 오는데 잔잔한 곡 틀어주세요") is None


def test_station_engine_quotes_request_without_ack(tmp_path):
    config = StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=30)
    settings = EngineSettings(
        audio_dir=tmp_path / "audio",
        sqlite_path=tmp_path / "metrics.sqlite",
        safety_rules_path=RULES,
        target_buffer_sec=1e9,
    )
    transport = CaptureTransport()
    engine = StationEngine(config, settings, transport)

    async def scenario() -> ListenerRequest:
        req = await engine.submit_request("story", "요즘 잠이 안 와요", "tester")
        await engine.run(max_segments=6)
        return req

    req = asyncio.run(scenario())

    # 요청별 접수 확인(ack)은 보내지 않는다 (CORNER_DESIGN D5) — 인용 자체가 반영이다
    assert all(s.priority == 50 for s in transport.segments)
    quoted = [s for s in transport.segments if s.request_ref == req.request_id]
    assert len(quoted) == 1 and quoted[0].corner_type == "opening"
    assert (req.request_id, RequestState.GENERATED) in transport.states
    assert transport.station_events[0][0] == "station.started"
