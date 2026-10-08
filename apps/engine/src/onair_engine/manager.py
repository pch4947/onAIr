"""EngineManager — 다중 스테이션 수명주기 (설계 문서 3장, 확인 9 결정).

다중 스테이션 동시 운영은 검증 필수 범위다. 로컬 실행(run_local)은 설정 파일의 방 하나를 띄우고,
운영(serve)은 engine:control의 station.created마다 방을 하나씩 띄운다 (계약 3.3절).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Protocol

from pydantic import ValidationError

from .domain import StationConfig, StationProfile
from .engine import EngineSettings, StationEngine, StationRejected
from .pipeline.tts import list_cartesia_voices
from .transport import Transport, make_transport

try:
    from onair_schema import StationCreated, error_detail
except ImportError as exc:  # 공용 패키지 — 엔진 의존성 설치와 별도로 깔아야 한다
    raise ImportError("onair_schema가 필요합니다: pip install -e ../../packages/onair_schema "
                      "(apps/engine 기준)") from exc

logger = logging.getLogger(__name__)

EVENT_RETRY_SEC = 3.0


class ControlChannel(Protocol):
    def events(self) -> AsyncIterator[dict]: ...


def config_from_created(station_id: str, created: StationCreated, raw: dict) -> StationConfig:
    """검증된 station.created → 엔진 내부 설정. persona의 표현 층만 프롬프트용 프로필로 옮긴다."""
    p = created.persona
    return StationConfig(
        station_id=station_id,
        profile=StationProfile(
            dj_name=p.dj_name, tone=p.tone, concept=p.concept,
            examples=list(p.examples), forbidden=list(p.forbidden),
            signature_phrases=list(p.signature_phrases),
        ),
        broadcast_minutes=created.broadcast_minutes,
        policy_name=created.policy,
        persona_id=p.persona_id,
        voice=p.voice,
        topic=created.topic,
        first_song=created.first_song.model_dump() if created.first_song else None,
        snapshot=raw,
    )


class EngineManager:
    def __init__(self, settings: EngineSettings,
                 transport_factory: Callable[[str], Transport] | None = None,
                 voice_catalog: Callable[[], Iterable[str]] | None = None):
        """voice_catalog: TTS 제공자의 보이스 ID 목록. None이면 cartesia일 때만 API로 조회하고,
        다른 제공자(로컬 테스트용 dummy·edge·google)는 보이스를 검사하지 않는다."""
        self.settings = settings
        self._transport_factory = transport_factory or self._make_transport
        if voice_catalog is None and settings.tts == "cartesia":
            voice_catalog = lambda: [v["id"] for v in list_cartesia_voices()]
        self._voice_catalog = voice_catalog
        self._voice_ids: set[str] | None = None
        self._stations: dict[str, StationEngine] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._event_tasks: dict[str, asyncio.Task] = {}

    @property
    def stations(self) -> dict[str, StationEngine]:
        return dict(self._stations)

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
        """방 하나를 띄운다. 준비가 안 되면(키·보이스) StationRejected — 방송 태스크는 만들지 않는다."""
        if config.station_id in self._stations:
            raise ValueError(f"station already running: {config.station_id}")
        transport = self._transport_factory(config.station_id)
        try:
            engine = StationEngine(config, self.settings, transport)
        except Exception:
            await _close(transport)
            raise
        self._stations[config.station_id] = engine
        self._tasks[config.station_id] = asyncio.create_task(
            self._run_station(engine, max_segments)
        )
        self._event_tasks[config.station_id] = asyncio.create_task(self._consume_events(engine))
        return engine

    async def close_station(self, station_id: str) -> None:
        engine = self._stations.get(station_id)
        if engine is None:
            return
        engine.stop()
        await self._tasks[station_id]

    async def close_all(self) -> None:
        await asyncio.gather(*(self.close_station(sid) for sid in list(self._stations)))

    async def _run_station(self, engine: StationEngine, max_segments: int | None) -> None:
        """방송이 어떻게 끝나든(정상 종료·station.closed·시작 실패) 방을 정리한다."""
        station_id = engine.config.station_id
        try:
            await engine.run(max_segments=max_segments)
        except StationRejected as exc:  # station.rejected는 engine.run이 이미 보냈다
            logger.warning("STATION_REJECTED %s %s", station_id, exc)
        except Exception:
            logger.exception("STATION_FAILED %s", station_id)
        finally:
            self._stations.pop(station_id, None)
            self._tasks.pop(station_id, None)
            await self._release(station_id, engine)

    # ── 운영: engine:control ─────────────────────────────────────────

    async def serve(self, control: ControlChannel) -> None:
        """engine:control을 끝없이 소비한다. 연결이 끊기면 잠시 뒤 다시 구독한다 (_consume_events와 같은 규칙)."""
        try:
            while True:
                try:
                    async for event in control.events():
                        try:
                            await self.handle_event(event)
                        except Exception:
                            logger.exception("CONTROL_EVENT_FAILED %s", event.get("type"))
                except Exception:
                    logger.exception("CONTROL_LOOP_FAILED")
                await asyncio.sleep(EVENT_RETRY_SEC)
        finally:
            await self.close_all()

    async def handle_event(self, event: dict) -> None:
        """engine:control 이벤트 라우팅 (계약 3.2절 — 이 스트림에는 station.created만 온다)."""
        if event.get("type") == "station.created":
            await self._on_station_created(event)
        elif event.get("type"):
            logger.info("CONTROL_EVENT_IGNORED %s", event.get("type"))

    async def _on_station_created(self, event: dict) -> None:
        station_id = event.get("station_id")
        if not station_id:  # 응답을 보낼 스트림조차 정할 수 없다 — 기록만 남긴다
            logger.error("STATION_CREATED_WITHOUT_ID %s", event.get("event_id"))
            return
        if station_id in self._stations:
            # at-least-once 재전달 — 이미 떠 있는 방을 다시 띄우지 않는다
            logger.info("STATION_CREATED_DUPLICATE %s", station_id)
            return
        raw = event.get("payload") or {}
        try:
            created = StationCreated.model_validate(raw)
            if not await self._voice_known(created.persona.voice):
                raise StationRejected(
                    "unknown_voice",
                    f"persona.voice: {created.persona.voice} — {self.settings.tts} 보이스 목록에 없음")
            await self.start_station(config_from_created(station_id, created, raw))
        except ValidationError as exc:
            await self._reject(station_id, "invalid_payload", error_detail(exc))
        except StationRejected as exc:
            await self._reject(station_id, exc.reason, exc.detail)
        else:
            logger.info("STATION_STARTING %s persona=%s", station_id, created.persona.persona_id)

    async def _voice_known(self, voice: str) -> bool:
        """보이스 목록은 한 번 받아 두고, 모르는 ID가 오면 한 번만 다시 받는다 (새로 추가된 보이스)."""
        if self._voice_catalog is None:
            return True
        if self._voice_ids is not None and voice in self._voice_ids:
            return True
        try:
            self._voice_ids = set(await asyncio.to_thread(self._voice_catalog))
        except Exception as exc:
            raise StationRejected("tts_unavailable", f"보이스 목록 조회 실패: {exc}"[:300]) from exc
        return voice in self._voice_ids

    async def _reject(self, station_id: str, reason: str, detail: str) -> None:
        logger.warning("STATION_REJECTED %s %s: %s", station_id, reason, detail)
        transport = self._transport_factory(station_id)
        try:
            await transport.notify_station("station.rejected", {"reason": reason, "detail": detail})
        finally:
            await _close(transport)

    # ── 로컬 실행 ────────────────────────────────────────────────────

    async def run_local(self, config: StationConfig, max_segments: int | None = None,
                        demo_requests: list[str] | None = None) -> None:
        """로컬 실행: 설정 파일의 방 하나를 띄우고 종료까지 대기한다."""
        engine = await self.start_station(config, max_segments=max_segments)
        task = self._tasks[config.station_id]
        if demo_requests:
            asyncio.create_task(self._inject_demo(engine, demo_requests))
        await task

    # ── 스테이션 이벤트 (:in) ─────────────────────────────────────────

    async def _release(self, station_id: str, engine: StationEngine) -> None:
        event_task = self._event_tasks.pop(station_id, None)
        if event_task is not None:
            event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await event_task
        await _close(engine.transport)

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
            engine.stop()  # 방송 태스크가 끝나면 _run_station이 방을 정리한다
        elif event_type:
            # TODO(M3): backpressure, state.transition — 정책 컨텍스트에 반영
            logger.info("EVENT_IGNORED %s", event_type)

    async def _inject_demo(self, engine: StationEngine, bodies: list[str]) -> None:
        # 관통 확인용 가짜 요청 — 백엔드 request.arrived 연동(M3) 전까지의 대체물
        await asyncio.sleep(2)
        for body in bodies:
            await engine.submit_request("story", body, requester_ref="demo_user")
            await asyncio.sleep(1)


async def _close(transport) -> None:
    close = getattr(transport, "close", None)
    if close is not None:
        await close()
