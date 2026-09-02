"""EngineManager — 다중 스테이션 수명주기 (설계 문서 3장, 확인 9 결정).

다중 스테이션 동시 운영은 검증 필수 범위다. M0은 로컬 설정 파일의 방 하나로
시작하고, M3에서 station.created/closed 이벤트 기반 수명주기를 실구현한다.
"""
from __future__ import annotations

import asyncio

from .domain import StationConfig
from .engine import EngineSettings, StationEngine
from .transport import make_transport


class EngineManager:
    def __init__(self, settings: EngineSettings):
        self.settings = settings
        self._stations: dict[str, StationEngine] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def start_station(self, config: StationConfig,
                            max_segments: int | None = None) -> StationEngine:
        if config.station_id in self._stations:
            raise ValueError(f"station already running: {config.station_id}")
        transport = make_transport(self.settings.transport_kind)
        engine = StationEngine(config, self.settings, transport)
        self._stations[config.station_id] = engine
        self._tasks[config.station_id] = asyncio.create_task(
            engine.run(max_segments=max_segments)
        )
        return engine

    async def close_station(self, station_id: str) -> None:
        engine = self._stations.pop(station_id, None)
        if engine is None:
            return
        engine.stop()
        await self._tasks.pop(station_id)

    async def handle_event(self, event: dict) -> None:
        """백엔드 이벤트 라우팅 (설계 문서 5.2) — TODO(M3): station.created/closed 등."""
        raise NotImplementedError("M3: 백엔드 이벤트 기반 수명주기")

    async def run_local(self, config: StationConfig, max_segments: int | None = None,
                        demo_requests: list[str] | None = None) -> None:
        """M0 로컬 실행: 설정 파일의 방 하나를 띄우고 종료까지 대기한다."""
        engine = await self.start_station(config, max_segments=max_segments)
        task = self._tasks[config.station_id]
        if demo_requests:
            asyncio.create_task(self._inject_demo(engine, demo_requests))
        try:
            await task
        finally:
            self._stations.pop(config.station_id, None)
            self._tasks.pop(config.station_id, None)

    async def _inject_demo(self, engine: StationEngine, bodies: list[str]) -> None:
        # 관통 확인용 가짜 요청 — 백엔드 request.arrived 연동(M3) 전까지의 대체물
        await asyncio.sleep(2)
        for body in bodies:
            await engine.submit_request("story", body, requester_ref="demo_user")
            await asyncio.sleep(1)
