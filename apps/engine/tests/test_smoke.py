"""M0 관통 스모크 테스트 — 더미 어댑터로 파이프라인·엔진이 끝까지 도는지 확인."""
import asyncio
from pathlib import Path

from onair_engine.catalog import Catalog
from onair_engine.corners import build_corners
from onair_engine.domain import (
    GenerationJob,
    Material,
    SegmentKind,
    StationConfig,
    StationProfile,
)
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.pipeline.llm import make_llm
from onair_engine.pipeline.pipeline import GenerationPipeline
from onair_engine.pipeline.safety import SafetyChecker
from onair_engine.pipeline.tts import make_tts
from onair_engine.sources.rss import RssCollector
from onair_engine.telemetry import Telemetry

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"
PROFILE = StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송")


class CaptureTransport:
    def __init__(self):
        self.segments = []
        self.states = []

    async def publish_segment(self, sub):
        self.segments.append(sub)

    async def notify_request_state(self, request_id, state):
        self.states.append((request_id, state))

    async def events(self):
        while True:
            await asyncio.sleep(3600)
            yield {}


def make_pipeline(tmp_path):
    return GenerationPipeline(
        station_id="st_test", profile=PROFILE,
        corners=build_corners(catalog=Catalog(), rss=RssCollector()),
        llm=make_llm("dummy"), tts=make_tts("dummy"), safety=SafetyChecker(RULES),
        telemetry=Telemetry(tmp_path / "metrics.sqlite", "st_test"),
        audio_dir=tmp_path / "audio",
    )


def test_dummy_pipeline_produces_segment(tmp_path):
    pipeline = make_pipeline(tmp_path)
    job = GenerationJob(job_id="job_t1", corner_type="filler", material=Material())
    sub = asyncio.run(pipeline.run(job))
    assert sub is not None
    assert sub.duration_ms > 0
    assert Path(sub.audio_ref).exists()
    assert sub.kind == SegmentKind.FILLER


def test_l0_blocks_contact_info():
    safety = SafetyChecker(RULES)
    assert safety.check_l0("제 번호는 010-1234-5678 이에요") is not None
    assert safety.check_l0("오늘 비 오는데 잔잔한 곡 틀어주세요") is None


def test_station_engine_handles_request_end_to_end(tmp_path):
    config = StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=1)
    settings = EngineSettings(
        audio_dir=tmp_path / "audio",
        sqlite_path=tmp_path / "metrics.sqlite",
        safety_rules_path=RULES,
    )
    transport = CaptureTransport()
    engine = StationEngine(config, settings, transport)

    async def scenario():
        await engine.submit_request("story", "요즘 잠이 안 와요", "tester")
        await engine.run(max_segments=6)

    asyncio.run(scenario())

    kinds = [s.kind for s in transport.segments]
    assert SegmentKind.ACK in kinds, "요청에 대한 접수 확인(ack)이 송출되어야 한다"
    replies = [s for s in transport.segments if s.corner_type == "request_reply"]
    assert replies and replies[0].request_ref, "요청 본 답변 세그먼트가 제출되어야 한다"
