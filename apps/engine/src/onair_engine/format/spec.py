"""형식 · 코너 · 단계 정의 — 코드가 아니라 설정(config/formats/*.yaml)에서 온다 (docs/CORNER_DESIGN.md 3·7절).

코너를 더하거나 수치를 바꿀 때 엔진 본체는 고치지 않는다. 여기 모델은 yaml을 검증하는 그릇이다.
"""
from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Mode(StrEnum):
    """입력 종류별 반영 모드 (CORNER_DESIGN 4.2·4.3절). DEFER·NONE은 모드가 아니라 라우팅 지시다."""

    QUOTE = "quote"
    STEER = "steer"
    READ = "read"
    FEATURE = "feature"
    SOLICIT = "solicit"
    CLOSED = "closed"
    DEFER = "defer"  # 그 모드를 가진 다음 코너로 넘긴다
    NONE = "none"  # 이 코너는 쓰지 않는다 (입력은 그대로 남는다)


InputKind = Literal["chat", "message", "board_story", "song_request", "vote"]
# 단계가 소비하는 항목의 출처 — corners/run.py MaterialSources가 공급한다
# talk_point는 이야기 단계의 말할 거리 하나 — 채팅, 다시 꺼낼 청취자 이야기, 갈래의 말할 거리 순 (scheduler가 공급)
ItemSource = Literal["track", "next_track", "news", "covered_news", "board_story", "message", "vote",
                     "talk_point"]

# 채팅을 인용할 수 있는 모드
QUOTABLE = (Mode.QUOTE, Mode.STEER)
_NOT_REFLECTED = (Mode.CLOSED, Mode.DEFER, Mode.NONE)


class StepSpec(BaseModel):
    """세그먼트 1개를 만드는 단위. items가 있으면 항목을 take개씩 꺼내 최대 repeat번 반복한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str  # corners/steps.py의 단계 유형, 또는 "glue"(사전 렌더 문구)
    glue: Literal["call", "close", "resume"] | None = None
    # 최대 반복 횟수. 문자열이면 참여 강도 프리셋의 값을 쓴다 (예: text_count)
    repeat: int | str = 1
    items: ItemSource | None = None
    take: int = Field(default=1, ge=1, le=5)
    # 같은 방송에서 이 코너에 두 번째 들어올 때부터 대신 쓸 단계 유형(glue면 문구) — 주제를
    # 매번 처음부터 소개하지 않게 한다 (CORNER_DESIGN 7.7절)
    again: str | None = None

    @model_validator(mode="after")
    def _glue_has_key(self) -> StepSpec:
        if (self.kind == "glue") != (self.glue is not None):
            raise ValueError("glue 단계는 kind: glue와 glue: call|close|resume을 함께 쓴다")
        if self.kind == "glue" and self.again not in (None, "call", "close", "resume"):
            raise ValueError(f"glue 단계의 again은 call|close|resume이다: {self.again}")
        return self

    def on_reentry(self) -> StepSpec:
        """다시 들어온 코너에서 쓸 단계. again이 없으면 그대로다."""
        if self.again is None:
            return self
        return self.model_copy(update={"glue" if self.kind == "glue" else "kind": self.again})


class CornerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    corner_type: str = ""  # 로더가 yaml 키로 채운다
    title: str
    steps: list[StepSpec] = Field(min_length=1)
    contract: dict[InputKind, Mode] = Field(default_factory=dict)
    budget_sec: tuple[int, int] = (60, 240)
    # 접착 요소 문구 — {title}, {dj} 슬롯. 방송 시작 시 한 번 합성해 재사용한다 (GlueBank)
    call: str | None = None
    close: str | None = None
    resume: str | None = None  # 다시 들어올 때의 콜 (단계의 again: resume)
    # 진입 조건 — 항목 출처별 최소 개수. 못 채우면 fallback 코너로 가거나 이번 차례를 건너뛴다
    requires: dict[ItemSource, int] = Field(default_factory=dict)
    fallback: str | None = None
    # 뉴스 형식 — 주관 표현 금지. persona의 유머·입버릇을 끈다 (CORNER_DESIGN 6.4절)
    subjectivity: Literal["allowed", "none"] = "allowed"

    def mode(self, kind: InputKind) -> Mode:
        return self.contract.get(kind, Mode.DEFER)

    def glue_text(self, which: str) -> str | None:
        return {"call": self.call, "close": self.close, "resume": self.resume}.get(which)


class Intensity(BaseModel):
    """참여 강도 프리셋 (CORNER_DESIGN 8.1절). 선별 기준·창 길이는 넣지 않는다 — 실험 조작 변수다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    quote_limit: int = Field(ge=0, le=2)  # 멘트당 인용 상한
    quote_gap_sec: int = Field(ge=0)  # 인용 간 최소 간격
    steer_limit: int = Field(ge=0)  # 코너당 화제 전환 상한
    question: bool  # topic_talk에서 청취자에게 질문 던지기
    text_count: int = Field(ge=1, le=10)  # text_corner 낭독 건수


class FormatSpec(BaseModel):
    """방송 형식 하나. cycle의 $main은 호스트가 고른 본 코너를 번갈아, $section은 뉴스 분야마다 펼친다."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["music", "talk", "news"]
    label: str
    guide: str = ""  # 오프닝의 참여 방법 안내 — 형식의 계약에서 정한 고정 문장
    opening: str = "opening"
    closing: str = "closing"
    # 방송 중에 이 형식으로 바꿔 들어올 때 먼저 들어갈 코너 (#260). 없으면 순환의 첫 코너부터다
    entry_corner: str | None = None
    cycle: list[str] = Field(min_length=1)
    host_corners: list[str] = Field(default_factory=list)  # $main 후보
    default_main: list[str] = Field(default_factory=list)  # 호스트가 고르지 않았을 때 (계약 v2 기본값과 같게)
    default_sections: list[str] = Field(default_factory=list)  # 뉴스 분야 기본값
    section_names: dict[str, str] = Field(default_factory=dict)  # 분야 코드 → 방송에서 부르는 이름
    corners: dict[str, CornerSpec]
    intensity: dict[Literal["low", "mid", "high"], Intensity]
    track_sec_estimate: int = 240  # 곡 길이를 모를 때 클락 계산에 쓰는 값 (ENGINE_ARCHITECTURE 4.3절)
    # false면 곡 사이에 곡 소개 말고 코너가 하나는 있어야 한다 — 사이 코너가 다 빠졌으면 다음 곡도 건너뛴다.
    # 음악 형식은 곡이 이어지는 것이 정상이라 true (CORNER_DESIGN 6.2절, #228)
    songs_back_to_back: bool = True

    @model_validator(mode="after")
    def _corners_exist(self) -> FormatSpec:
        names = [self.opening, self.closing, *self.host_corners, *self.default_main,
                 *(c for c in self.cycle if not c.startswith("$")),
                 *(c.split(":", 1)[1] for c in self.cycle if c.startswith("$section:"))]
        names += [c.fallback for c in self.corners.values() if c.fallback]
        names += [self.entry_corner] if self.entry_corner else []
        missing = sorted({n for n in names if n not in self.corners})
        if missing:
            raise ValueError(f"정의되지 않은 코너: {missing}")
        if "$main" in self.cycle and not self.host_corners:
            raise ValueError("$main을 쓰려면 host_corners가 필요하다")
        bad = [c for c in self.cycle if c.startswith("$") and c != "$main"
               and not c.startswith("$section:")]
        if bad:
            raise ValueError(f"알 수 없는 자리 표시: {bad} ($main, $section:<코너>만 쓴다)")
        return self

    @model_validator(mode="after")
    def _repeat_keys_exist(self) -> FormatSpec:
        keys = set(Intensity.model_fields)
        bad = sorted({f"{c.corner_type}.{s.kind}: {s.repeat}" for c in self.corners.values()
                      for s in c.steps if isinstance(s.repeat, str) and s.repeat not in keys})
        if bad:
            raise ValueError(f"repeat에 쓴 참여 강도 키가 없다: {bad}")
        return self

    def corner(self, name: str) -> CornerSpec:
        return self.corners[name]

    def accepts(self, kind: InputKind) -> bool:
        """이 형식이 방송에 반영하는 입력 종류인가 — 아니면 NOT_ON_AIR다 (CORNER_DESIGN 4.4절)."""
        return any(self.corners[name].mode(kind) not in _NOT_REFLECTED for name in self._reachable())

    def _reachable(self) -> set[str]:
        # 공통 파일의 코너가 합쳐져 있으므로, 이 형식이 실제로 쓰는 코너만 본다
        names = {self.opening, self.closing}
        for item in self.cycle:
            if item == "$main":
                names.update(self.host_corners)
            elif item.startswith("$section:"):
                names.add(item.split(":", 1)[1])
            else:
                names.add(item)
        names.update(c for n in list(names) if (c := self.corners[n].fallback))
        return names
