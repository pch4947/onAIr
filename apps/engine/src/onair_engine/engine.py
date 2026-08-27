"""StationEngine — 스테이션 1개의 컴포넌트 조립과 수명 관리 (설계 문서 3장)."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from .ack import AckCache
from .catalog import Catalog
from .corners import build_corners
from .domain import ListenerRequest, RequestState, StationConfig, new_id
from .pipeline.llm import make_llm
from .pipeline.pipeline import GenerationPipeline
from .pipeline.safety import SafetyChecker
from .pipeline.tts import make_tts
from .scheduler.policies import get_policy
from .scheduler.running_order import FixedRunningOrderBuilder
from .scheduler.scheduler import Scheduler
from .sources.rss import RssCollector
from .telemetry import Telemetry
from .transport import Transport


@dataclass
class EngineSettings:
    """스테이션과 무관한 엔진 인프라 설정 (설정 파일의 station 외 항목)."""

    transport_kind: str = "stdout"
    audio_dir: Path = field(default_factory=lambda: Path("var/audio"))
    sqlite_path: Path = field(default_factory=lambda: Path("var/engine_metrics.sqlite"))
    safety_rules_path: Path = field(default_factory=lambda: Path("config/safety_rules.yaml"))
    llm: str = "dummy"
    tts: str = "dummy"
    max_concurrent_generations: int = 2
    target_buffer_sec: float = 30.0


class StationEngine:
    def __init__(self, config: StationConfig, settings: EngineSettings, transport: Transport):
        self.config = config
        self.settings = settings
        self.transport = transport
        self.telemetry = Telemetry(settings.sqlite_path, config.station_id)

        audio_dir = Path(settings.audio_dir) / config.station_id
        tts = make_tts(settings.tts)
        safety = SafetyChecker(settings.safety_rules_path)
        corners = build_corners(catalog=Catalog(), rss=RssCollector())
        pipeline = GenerationPipeline(
            station_id=config.station_id, profile=config.profile, corners=corners,
            llm=make_llm(settings.llm), tts=tts, safety=safety,
            telemetry=self.telemetry, audio_dir=audio_dir,
        )
        self.ack_cache = AckCache(tts=tts, audio_dir=audio_dir)
        self.scheduler = Scheduler(
            config=config,
            order=FixedRunningOrderBuilder().build(config),
            policy=get_policy(
                config.policy_name,
                target_buffer_sec=settings.target_buffer_sec,
                max_inflight=settings.max_concurrent_generations,
            ),
            corners=corners, pipeline=pipeline, ack_cache=self.ack_cache,
            transport=transport, telemetry=self.telemetry,
            max_concurrent=settings.max_concurrent_generations,
        )
        self._safety = safety
        self._stop = asyncio.Event()

    async def run(self, max_segments: int | None = None) -> None:
        # ack 캐시는 방송 시작 전에 이 스테이션의 보이스로 사전 렌더링한다 (설계 문서 4.5)
        await self.ack_cache.prerender()
        await self.scheduler.run(self._stop, max_segments=max_segments)
        self.telemetry.close()

    def stop(self) -> None:
        self._stop.set()

    async def submit_request(self, kind: str, body: str, requester_ref: str) -> ListenerRequest:
        """청취자 요청 수신 — L0 입력단 검사 후 큐 적재 (설계 문서 4.6).

        M3에서 transport.events()의 request.arrived가 이 메서드로 라우팅된다.
        """
        req = ListenerRequest(
            request_id=new_id("req"), kind=kind, body=body,
            requester_ref=requester_ref, received_at=time.time(),
        )
        if self._safety.check_l0(body) is not None:
            req.state = RequestState.REJECTED
            self.telemetry.log_request_state(req.request_id, req.state)
            await self.transport.notify_request_state(req.request_id, req.state)  # F-15
            return req
        req.state = RequestState.SCREENED
        self.telemetry.log_request_state(req.request_id, req.state)
        await self.transport.notify_request_state(req.request_id, req.state)
        self.scheduler.enqueue_request(req)
        return req
