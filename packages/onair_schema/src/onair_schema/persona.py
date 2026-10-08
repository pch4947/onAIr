"""DJ persona — 표현 층 (docs/persona.md 2.1·2.3절).

행동 층(BehaviorSpec)은 v1 페이로드에 넣지 않는다 — 정책 A/B/C 확정 후 추가하고 contract_version을 올린다.
"""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


def text(max_length: int):
    """앞뒤 공백을 지운 뒤 1~max_length자 — 공백만 있는 값은 빈 값으로 거부된다."""
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                            max_length=max_length)]


Id64 = text(64)
Genre = text(20)  # TODO: 고정 장르 목록 확정 시 Literal로 좁힌다 (계약 9.3절 장르 라벨링 미결)
DjName = text(20)
Concept = text(60)
Tone = text(100)
Example = text(150)
Signature = text(30)
Forbidden = text(50)


class Style(BaseModel):
    """호스트가 폼에서 고른 말투 선택지. 프롬프트용 tone 문장과 별도로 남긴다 — 평가의 말투 이탈 기준이다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    formality: Literal["polite", "casual"]
    energy: Literal["low", "mid", "high"]
    humor: Literal["rare", "some", "often"]


class Persona(BaseModel):
    """방 생성 화면에서 엔진 초안을 호스트가 고르고 고친 결과. 백엔드 DB에 저장된 값 그대로 온다.

    목록 필드는 JSON 배열만 받는다 — 문자열 하나가 오면 거부한다 (한 글자씩 쪼개지는 사고 방지).
    voice가 TTS 제공자의 보이스 목록에 있는지는 스키마가 아니라 엔진이 검사한다.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    persona_id: Id64 | None = None  # 백엔드 DB가 발급 — 초안 단계에는 없다
    style: Style
    music_taste: list[Genre] = Field(default_factory=list, max_length=3)
    voice: Id64
    dj_name: DjName
    concept: Concept
    tone: Tone
    examples: list[Example] = Field(min_length=3, max_length=5)
    signature_phrases: list[Signature] = Field(default_factory=list, max_length=3)
    forbidden: list[Forbidden] = Field(default_factory=list, max_length=10)
