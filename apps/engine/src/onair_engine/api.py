"""엔진 REST — 계약 6장 (docs/ENGINE_REDIS_CONTRACT.md).

방송 흐름(제출·요청·상태)은 Redis다. 여기는 푸시로 감당이 안 되는 조회와, 호스트가 화면에서 결과를
기다리는 동기 요청(persona 초안)만 둔다. 루프백에만 열고 /health를 뺀 /v1은 Bearer 토큰을 요구한다.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from typing import Annotated

try:
    from fastapi import Depends, FastAPI, Header, HTTPException, Query
    from fastapi.responses import JSONResponse
except ImportError as exc:  # 선택 의존성 — 기동 시점에 실패시킨다
    raise RuntimeError('엔진 REST를 쓰려면 pip install -e ".[api]" 가 필요합니다') from exc
from onair_schema import PersonaForm
from pydantic import BaseModel, Field

from .engine import EngineSettings, StationRejected
from .manager import EngineManager
from .personas import DraftsFailed, FormRejected, PersonaDrafter
from .pipeline.llm import LlmError
from .pipeline.tts import list_cartesia_voices
from .telemetry import read_decisions, recent_latency_p95_ms

logger = logging.getLogger(__name__)


class DraftsRequest(BaseModel):
    form: PersonaForm
    count: int = Field(default=3, ge=1, le=5)


class CheckRequest(BaseModel):
    persona: dict  # 스키마 검증은 직접 해서 계약 형식({"errors": [{"field", "reason"}]})으로 돌려준다


# 더미 TTS(무음)일 때의 보이스 목록 — 방 생성 화면을 API 키 없이 개발할 수 있게. 보이스 검사도 하지 않는다
DUMMY_VOICES = [
    {"id": "dummy-feminine", "name": "더미 여성", "gender": "feminine",
     "description": "개발용 — 무음", "native": True},
    {"id": "dummy-masculine", "name": "더미 남성", "gender": "masculine",
     "description": "개발용 — 무음", "native": True},
]
VOICE_CACHE_SEC = 600.0  # 보이스 목록은 거의 안 바뀐다 — 폼을 열 때마다 제공자를 부르지 않는다


def default_voice_lister(tts: str) -> Callable[[], list[dict]]:
    if tts == "cartesia":
        return list_cartesia_voices
    if tts == "dummy":
        return lambda: DUMMY_VOICES
    return list  # 빈 목록 — google·edge는 청취 테스트용이라 폼에서 고르지 않는다


def _errors(status: int, errors: list[dict]) -> JSONResponse:
    return JSONResponse(status_code=status, content={"errors": errors})


def create_app(manager: EngineManager, drafter: PersonaDrafter, settings: EngineSettings,
               token: str | None, voice_lister: Callable[[], list[dict]] | None = None) -> FastAPI:
    voice_lister = voice_lister or default_voice_lister(settings.tts)
    voice_cache: dict = {}  # {"at": 받은 시각, "voices": 목록}
    app = FastAPI(title="onAIr engine", version="v1", docs_url="/v1/docs",
                  openapi_url="/v1/openapi.json")

    def authorized(authorization: Annotated[str | None, Header()] = None) -> None:
        if token is None:  # 토큰을 안 정한 로컬 개발 — 루프백 바인딩만 믿는다
            return
        if not authorization or not secrets.compare_digest(authorization, f"Bearer {token}"):
            raise HTTPException(401, "invalid token")

    v1 = [Depends(authorized)]

    async def voice_known(voice: str) -> bool:
        try:
            return await manager.voice_known(voice)
        except StationRejected as exc:  # 보이스 목록 조회 실패 — 제공자 장애
            raise HTTPException(503, exc.detail) from None

    @app.get("/health")
    async def health():
        return {"ok": True, "stations": len(manager.stations)}

    @app.get("/v1/voices", dependencies=v1)
    async def voices(native_only: bool = True):
        """방 생성 폼의 보이스 선택지 — 엔진 TTS 제공자의 한국어 보이스.

        native_only(기본): 한국어 원어민 보이스만. false면 한국어를 말할 수 있는 다국어 보이스도.
        """
        if not voice_cache or time.monotonic() - voice_cache["at"] > VOICE_CACHE_SEC:
            try:
                listed = await asyncio.to_thread(voice_lister)
            # RuntimeError: 키 누락·제공자 HTTP 오류, OSError: 연결 실패, ValueError: 응답 해석 불가
            except (RuntimeError, OSError, ValueError) as exc:
                logger.warning("VOICE_LIST_FAILED %s", exc)
                raise HTTPException(503, f"보이스 목록 조회 실패: {exc}"[:300]) from None
            voice_cache.update(at=time.monotonic(), voices=listed)
        listed = voice_cache["voices"]
        if native_only:
            listed = [v for v in listed if v.get("native", True)]
        return {"provider": settings.tts, "voices": listed}

    @app.get("/v1/engine/status", dependencies=v1)
    async def status():
        stations = manager.stations
        p95 = await asyncio.to_thread(recent_latency_p95_ms, settings.sqlite_path)
        return {
            "station_count": len(stations),
            "stations": [
                {"station_id": sid, "inflight_generations": e.scheduler.inflight,
                 "pending_requests": e.scheduler.pending_count}
                for sid, e in stations.items()
            ],
            "inflight_generations": sum(e.scheduler.inflight for e in stations.values()),
            # 상한은 방마다 따로 걸린다 — 전역 상한은 아직 없다 (계약 6.1절 "부분")
            "max_concurrent_per_station": settings.max_concurrent_generations,
            "recent_latency_p95_ms": p95,
            "llm": settings.llm,
            "tts": settings.tts,
        }

    @app.get("/v1/stations/{station_id}/decisions", dependencies=v1)
    async def decisions(station_id: str, after: Annotated[int, Query(ge=0)] = 0,
                        limit: Annotated[int, Query(ge=1, le=500)] = 100):
        rows = await asyncio.to_thread(read_decisions, settings.sqlite_path, station_id,
                                       after, limit)
        # 끝난 방송의 기록도 조회할 수 있어야 하므로 방이 떠 있는지는 따지지 않는다
        return {"decisions": rows, "next": rows[-1]["id"] if len(rows) == limit else None}

    @app.post("/v1/personas/drafts", dependencies=v1)
    async def drafts(body: DraftsRequest):
        if not await voice_known(body.form.voice):
            return _errors(422, [{"field": "form.voice",
                                  "reason": f"{settings.tts} 보이스 목록에 없습니다"}])
        try:
            async with asyncio.timeout(settings.draft_timeout_sec):
                personas = await drafter.drafts(body.form, body.count)
        except FormRejected as exc:
            return _errors(422, [{"field": exc.field, "reason": exc.reason}])
        except TimeoutError:
            raise HTTPException(503, "persona 초안 생성 시간 초과") from None
        except (LlmError, RuntimeError) as exc:  # RuntimeError: 키·모델 누락으로 LLM을 못 만듦
            logger.warning("PERSONA_DRAFTS_FAILED %s", exc)
            raise HTTPException(503, f"LLM 사용 불가: {exc}"[:300]) from None
        except DraftsFailed as exc:
            logger.warning("PERSONA_DRAFTS_INVALID %s", exc)
            raise HTTPException(503, f"쓸 수 있는 초안을 만들지 못했습니다: {exc}"[:300]) from None
        # 초안에는 persona_id가 없다 — 백엔드가 저장할 때 발급한다
        return {"drafts": [p.model_dump(exclude={"persona_id"}) for p in personas]}

    @app.post("/v1/personas/check", dependencies=v1)
    async def check(body: CheckRequest):
        errors = drafter.check(body.persona)
        voice = body.persona.get("voice")
        if not errors and isinstance(voice, str) and not await voice_known(voice):
            errors.append({"field": "persona.voice",
                           "reason": f"{settings.tts} 보이스 목록에 없습니다"})
        return _errors(422, errors) if errors else {"ok": True}

    return app


async def serve_api(app: FastAPI, settings: EngineSettings) -> None:
    import uvicorn

    config = uvicorn.Config(app, host=settings.api_host, port=settings.api_port,
                            log_level="warning", lifespan="off")
    logger.info("API_LISTENING http://%s:%s", settings.api_host, settings.api_port)
    await uvicorn.Server(config).serve()
