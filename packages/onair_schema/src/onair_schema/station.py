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


Format = Literal["music", "talk", "news"]
# 사연토크 형식에서 호스트가 고르는 본 코너 (docs/CORNER_DESIGN.md 8절)
TalkCorner = Literal["story_corner", "text_corner", "topic_talk", "dj_vs_listeners"]
NewsSection = Literal["general", "economy", "it", "sports", "culture"]  # 종합/경제/IT/스포츠/문화 [예시]
Intensity = Literal["low", "mid", "high"]
# corners·news_sections를 비웠을 때 — 백엔드·프론트가 형식 선택(#102)을 붙이기 전의 방도 그대로 뜬다
DEFAULT_TALK_CORNERS: tuple[str, ...] = ("story_corner", "topic_talk")
DEFAULT_NEWS_SECTIONS: tuple[str, ...] = ("general", "it")


class ExperimentSpec(BaseModel):
    """실험용 방에만 싣는 조건 (docs/persona.md 2.3절). 호스트 폼에는 노출하지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    condition: text(32)  # 조건 이름 — 분석 시 그룹 키
    selector: Literal["recency", "relevance", "diversity"]
    window_sec: int = Field(default=90, ge=30, le=300)  # 인용 후보 창 W [예시]
    binding: Literal["early", "late"] = "early"  # 선결합 / 지연 결합
    aggregate_ack: bool = True  # 집계 언급·예고 on/off (RQ-A 보조 조건)


class StationCreated(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    broadcast_minutes: int = Field(ge=30, le=60)  # MVP 30~60분 (결정 2026-10-08)
    first_song: Track | None = None  # 없으면 오프닝 곡 없이 바로 오프닝 멘트
    topic: text(60) | None = None  # 비우면 엔진이 컨셉에서 만든다
    persona: Persona
    # 방송 형식 — 계약 v2 개정안 10.2절. 기본값은 형식 선택이 없던 v1 방과 호환되게 둔다
    format: Format = "talk"
    corners: list[TalkCorner] | None = Field(default=None, min_length=2, max_length=3)
    news_sections: list[NewsSection] | None = Field(default=None, min_length=2, max_length=3)
    intensity: Intensity = "mid"
    experiment: ExperimentSpec | None = None
    # v1 필드 — 정책 분기는 코너 재설계로 없어졌다. 백엔드가 아직 보내므로 받기만 하고 쓰지 않는다
    policy: Literal["naive_fifo"] = "naive_fifo"

    @model_validator(mode="after")
    def _saved_persona(self) -> "StationCreated":
        # 초안(persona_id 없음)이 아니라 백엔드 DB에 저장된 persona여야 한다 — 계측 기록의 조인 키다
        if self.persona.persona_id is None:
            raise ValueError("persona.persona_id가 필요합니다 (백엔드 DB에 저장된 persona)")
        return self

    @model_validator(mode="after")
    def _fields_match_format(self) -> "StationCreated":
        if self.corners is not None:
            if self.format != "talk":
                raise ValueError("corners는 format이 talk일 때만 받는다")
            if len(set(self.corners)) != len(self.corners):
                raise ValueError("corners에 같은 코너가 두 번 있다")
        if self.news_sections is not None:
            if self.format != "news":
                raise ValueError("news_sections는 format이 news일 때만 받는다")
            if len(set(self.news_sections)) != len(self.news_sections):
                raise ValueError("news_sections에 같은 분야가 두 번 있다")
        return self

    def talk_corners(self) -> list[str]:
        return list(self.corners or DEFAULT_TALK_CORNERS)

    def sections(self) -> list[str]:
        return list(self.news_sections or DEFAULT_NEWS_SECTIONS)


def error_detail(exc: ValidationError, limit: int = 3) -> str:
    """station.rejected.detail — 사람이 읽는 원인. 예: 'persona.examples: List should have at least 3 items'."""
    parts = []
    for err in exc.errors()[:limit]:
        loc = ".".join(str(p) for p in err["loc"])
        parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
    more = len(exc.errors()) - limit
    return "; ".join(parts) + (f" (외 {more}건)" if more > 0 else "")
