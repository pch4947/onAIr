"""station.created 페이로드 (docs/ENGINE_REDIS_CONTRACT.md 3.3절).

첫 노래·방송 시간·오늘의 주제는 persona가 아니라 방의 속성이다 — 같은 persona를 여러 방이 쓸 수 있다.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .persona import Persona, text

# station.rejected.reason — 계약 3.1절
STATION_REJECT_REASONS = ("invalid_payload", "unknown_voice", "llm_unavailable", "tts_unavailable")


class Track(BaseModel):
    """곡 정보 (계약 3.4절). title·artist는 YouTube 메타데이터라 엔진은 데이터 블록으로만 프롬프트에 넣는다."""

    model_config = ConfigDict(extra="ignore", frozen=True)  # 백엔드가 곡 필드를 늘려도 엔진은 무시한다

    track_ref: text(128)  # YouTube면 yt:{video_id}
    title: text(200)
    artist: str = Field(default="", max_length=200)
    duration_ms: int = Field(gt=0)


class StationCreated(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    broadcast_minutes: int = Field(ge=30, le=60)  # MVP 30~60분 (결정 2026-10-08)
    first_song: Track | None = None  # 없으면 오프닝 곡 없이 바로 오프닝 멘트
    topic: text(60) | None = None  # 비우면 엔진이 컨셉에서 만든다
    persona: Persona
    policy: Literal["naive_fifo"] = "naive_fifo"  # 정책 A/B/C·코너별 대응표 확정 시 확장

    @model_validator(mode="after")
    def _saved_persona(self) -> "StationCreated":
        # 초안(persona_id 없음)이 아니라 백엔드 DB에 저장된 persona여야 한다 — 계측 기록의 조인 키다
        if self.persona.persona_id is None:
            raise ValueError("persona.persona_id가 필요합니다 (백엔드 DB에 저장된 persona)")
        return self


def error_detail(exc: ValidationError, limit: int = 3) -> str:
    """station.rejected.detail — 사람이 읽는 원인. 예: 'persona.examples: List should have at least 3 items'."""
    parts = []
    for err in exc.errors()[:limit]:
        loc = ".".join(str(p) for p in err["loc"])
        parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
    more = len(exc.errors()) - limit
    return "; ".join(parts) + (f" (외 {more}건)" if more > 0 else "")
