"""StationEngine — 스테이션 1개의 컴포넌트 조립과 수명 관리 (설계 문서 3장)."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from .ack import AckCache
from .audio import CachedTts
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
    transport_base_url: str = "http://localhost:3000"  # transport_kind="http"일 때 백엔드 주소
    transport_token: str | None = None  # 내부 통신 토큰 — 환경변수로 주입한다
    transport_redis_url: str = "redis://localhost:6379/0"  # transport_kind="redis"일 때
    audio_dir: Path = field(default_factory=lambda: Path("var/audio"))  # 공유 오디오 루트
    sqlite_path: Path = field(default_factory=lambda: Path("var/engine_metrics.sqlite"))
    safety_rules_path: Path = field(default_factory=lambda: Path("config/safety_rules.yaml"))
    llm: str = "dummy"
    llm_model: str | None = None  # None이면 어댑터 기본 모델 (openai는 필수)
    llm_base_url: str | None = None  # openai 호환 서버 주소. None이면 OpenAI 본가
    llm_reasoning_effort: str | None = None  # openai 추론 모델의 추론량 (지연 조절)
    tts: str = "dummy"
    tts_voice: str | None = None  # None이면 어댑터 기본 보이스
    tts_cache_dir: Path | None = None  # None이면 캐시 없이 매번 합성. 공유 오디오 루트 밖에 둔다
    max_concurrent_generations: int = 2
    target_buffer_sec: float = 30.0
    # 이보다 오래된 request.arrived는 답하지 않는다. 입력 스트림을 처음부터 읽으므로
    # 엔진이 꺼져 있던 사이의 예전 요청이 재기동 때 몰려온다 [예시]
    request_max_age_sec: float = 600.0


class StationRejected(Exception):
    """방송을 시작할 수 없다 — station.rejected로 백엔드에 돌려보낸다 (계약 3.1절)."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class StationEngine:
    def __init__(self, config: StationConfig, settings: EngineSettings, transport: Transport):
        self.config = config
        self.settings = settings
        self.transport = transport

        # 키·보이스 누락은 방송 도중이 아니라 여기서 실패한다 — 계측 DB를 열기 전에 확인한다
        try:
            # 보이스는 방마다 다르다 (persona.md 9.3절). 엔진 설정 값은 로컬 실행용 기본값이다
            tts_client = make_tts(settings.tts, voice=config.voice or settings.tts_voice)
        except RuntimeError as exc:
            raise StationRejected("tts_unavailable", str(exc)) from exc
        try:
            llm = make_llm(settings.llm, model=settings.llm_model,
                           base_url=settings.llm_base_url,
                           reasoning_effort=settings.llm_reasoning_effort)
        except RuntimeError as exc:
            raise StationRejected("llm_unavailable", str(exc)) from exc

        self.telemetry = Telemetry(settings.sqlite_path, config.station_id)
        if config.snapshot is not None:
            self.telemetry.log_station("station.created", config.snapshot)
        # 제출 페이로드의 audio_ref는 이 루트 기준 상대 경로로 나간다 (설계 문서 5.1)
        audio_root = Path(settings.audio_dir)
        # 파이프라인과 ack 모두 원자적 쓰기·캐시·길이 실측을 거친다
        tts = CachedTts(tts_client, cache_dir=settings.tts_cache_dir)
        safety = SafetyChecker(settings.safety_rules_path)
        corners = build_corners(catalog=Catalog(), rss=RssCollector())
        pipeline = GenerationPipeline(
            station_id=config.station_id, profile=config.profile, corners=corners,
            llm=llm, tts=tts, safety=safety,
            telemetry=self.telemetry, audio_root=audio_root,
            recent_segments=config.recent_segments,
        )
        self.ack_cache = AckCache(tts=tts, audio_root=audio_root, station_id=config.station_id)
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

    @property
    def topic(self) -> str:
        """이 방송의 확정된 주제 — station.started로 백엔드에 알린다 (백엔드는 이 주제로 사연 후보를 고른다).

        TODO(오프닝 코너): 호스트가 비웠으면 컨셉에서 LLM으로 만든다. 지금은 컨셉을 그대로 쓴다.
        """
        return self.config.topic or self.config.profile.concept

    async def run(self, max_segments: int | None = None) -> None:
        try:
            # ack 캐시는 방송 시작 전에 이 스테이션의 보이스로 사전 렌더링한다 (설계 문서 4.5)
            try:
                await self.ack_cache.prerender()
            except Exception as exc:  # 보이스 ID 오류·TTS 장애 — 첫 합성에서 드러난다
                await self.transport.notify_station("station.rejected", {
                    "reason": "tts_unavailable", "detail": f"ack 사전 렌더링 실패: {exc}"[:300],
                })
                raise StationRejected("tts_unavailable", str(exc)) from exc
            await self.transport.notify_station("station.started", {"topic": self.topic})
            self.telemetry.log_station("station.started", {"topic": self.topic})
            await self.scheduler.run(self._stop, max_segments=max_segments)
        finally:
            self.telemetry.close()

    def stop(self) -> None:
        self._stop.set()

    def report_backpressure(self, payload: dict, at: float | None = None) -> None:
        """백엔드의 실제 남은 방송 분량 — 편성 관리자가 생성량을 정하는 기준이 된다 (계약 3.2절)."""
        d_total_ms = payload.get("d_total_ms")
        if not isinstance(d_total_ms, (int, float)):  # 백엔드가 큐를 못 읽은 주기 — 이전 보고를 유지
            return
        self.scheduler.report_backpressure(d_total_ms, str(payload.get("severity", "")), at)

    async def reject_stale_request(self, request_id: str) -> None:
        """너무 늦게 받은 요청 — 답하지 않고 종결한다. 백엔드 상태가 requested로 남지 않게 rejected로 알린다."""
        self.telemetry.log_request_state(request_id, RequestState.REJECTED)
        await self.transport.notify_request_state(request_id, RequestState.REJECTED)

    async def submit_request(self, kind: str, body: str, requester_ref: str,
                             request_id: str | None = None) -> ListenerRequest:
        """청취자 요청 수신 — L0 입력단 검사 후 큐 적재 (설계 문서 4.6).

        백엔드 request.arrived 이벤트로 들어온 요청은 백엔드가 발급한 request_id를 그대로 쓴다.
        상태 통보를 백엔드가 자기 요청과 대응시킬 수 있어야 하기 때문이다. 데모 요청만 엔진이 발급한다.
        """
        req = ListenerRequest(
            request_id=request_id or new_id("req"), kind=kind, body=body,
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
