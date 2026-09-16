"""EngineManager — 다중 스테이션 수명주기 (설계 문서 3장, 확인 9 결정).

다중 스테이션 동시 운영은 검증 필수 범위다. M0은 로컬 설정 파일의 방 하나로
시작하고, M3에서 station.created/closed 이벤트 기반 수명주기를 실구현한다.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from .domain import StationConfig
from .engine import EngineSettings, StationEngine
from .transport import Transport, make_transport

logger = logging.getLogger(__name__)

EVENT_RETRY_SEC = 3.0


class EngineManager:
    def __init__(self, settings: EngineSettings,
                 transport_factory: Callable[[str], Transport] | None = None):
        self.settings = settings
        self._transport_factory = transport_factory or self._make_transport
        self._stations: dict[str, StationEngine] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._event_tasks: dict[str, asyncio.Task] = {}

    def _make_transport(self, station_id: str) -> Transport:
        return make_transport(
            self.settings.transport_kind,
            base_url=self.settings.transport_base_url,
            token=self.settings.transport_token,
            redis_url=self.settings.transport_redis_url,
            station_id=station_id,
        )

    async def start_station(self, config: StationConfig,
                            max_segments: int | None = None) -> StationEngine:
        if config.station_id in self._stations:
            raise ValueError(f"station already running: {config.station_id}")
        transport = self._transport_factory(config.station_id)
        engine = StationEngine(config, self.settings, transport)
        self._stations[config.station_id] = engine
        self._tasks[config.station_id] = asyncio.create_task(
            engine.run(max_segments=max_segments)
        )
        self._event_tasks[config.station_id] = asyncio.create_task(self._consume_events(engine))
        return engine

    async def close_station(self, station_id: str) -> None:
        engine = self._stations.pop(station_id, None)
        if engine is None:
            return
        engine.stop()
        await self._tasks.pop(station_id)
        await self._release(station_id, engine)

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
            await self._release(config.station_id, engine)

    async def _release(self, station_id: str, engine: StationEngine) -> None:
        event_task = self._event_tasks.pop(station_id, None)
        if event_task is not None:
            event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await event_task
        close = getattr(engine.transport, "close", None)
        if close is not None:
            await close()

    async def _consume_events(self, engine: StationEngine) -> None:
        """스테이션 이벤트 수신 루프 (설계 문서 5.2).

        이벤트 하나의 처리 실패가 루프를 끊지 않고, 연결이 끊기면 잠시 뒤 다시 구독한다.
        재구독 시 transport가 ACK 못 한 이벤트부터 다시 준다 (at-least-once).
        """
        while True:
            try:
                async for event in engine.transport.events():
                    try:
                        await self._route_station_event(engine, event)
                    except Exception:
                        logger.exception("EVENT_FAILED %s", event.get("type"))
            except Exception:
                logger.exception("EVENT_LOOP_FAILED %s", engine.config.station_id)
            await asyncio.sleep(EVENT_RETRY_SEC)

    async def _route_station_event(self, engine: StationEngine, event: dict) -> None:
        event_type = event.get("type")
        payload = event.get("payload") or {}
        if event_type == "request.arrived":
            await engine.submit_request(
                payload.get("kind", "story"), payload["body"],
                payload.get("requester_ref", "anonymous"),
                request_id=payload.get("request_id"),
            )
        elif event_type == "station.closed":
            engine.stop()
        elif event_type:
            # TODO(M3): backpressure, state.transition — 정책 컨텍스트에 반영
            logger.info("EVENT_IGNORED %s", event_type)

    async def _inject_demo(self, engine: StationEngine, bodies: list[str]) -> None:
        # 관통 확인용 가짜 요청 — 백엔드 request.arrived 연동(M3) 전까지의 대체물
        await asyncio.sleep(2)
        for body in bodies:
            await engine.submit_request("story", body, requester_ref="demo_user")
            await asyncio.sleep(1)
