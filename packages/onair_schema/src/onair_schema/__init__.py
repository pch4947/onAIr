"""onAIr 공용 스키마 — 백엔드(FastAPI)와 대본 엔진이 같은 정의로 검증한다 (docs/persona.md 2.3절).

필드와 제한은 docs/ENGINE_REDIS_CONTRACT.md 3.3절 표가 명세다. 제한 수치는 [예시]이며,
바꿀 때는 계약 문서와 함께 바꾼다.
"""
from .persona import Persona, PersonaForm, Style
from .station import (
    DEFAULT_NEWS_SECTIONS,
    DEFAULT_TALK_CORNERS,
    STATION_REJECT_REASONS,
    ExperimentSpec,
    StationCreated,
    Track,
    error_detail,
)

__all__ = ["DEFAULT_NEWS_SECTIONS", "DEFAULT_TALK_CORNERS", "STATION_REJECT_REASONS", "ExperimentSpec",
           "Persona", "PersonaForm", "StationCreated", "Style", "Track", "error_detail"]
