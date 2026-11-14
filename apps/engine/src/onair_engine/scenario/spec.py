"""시나리오 파일(config/scenarios/*.yaml) 형식 — 읽을 때 검증한다."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def parse_at(value: str | float) -> float:
    """방송 시작 기준 시각 — 초(숫자) 또는 "m:ss"."""
    if isinstance(value, (int, float)):
        return float(value)
    minutes, _, seconds = str(value).partition(":")
    return float(minutes) * 60 + float(seconds) if seconds else float(minutes)


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StationSpec(_Spec):
    format: Literal["talk", "music", "news"] = "talk"
    corners: list[str] | None = None
    intensity: Literal["low", "mid", "high"] = "mid"
    topic: str | None = None
    persona: str = "config/personas/saebyeok.yaml"  # 엔진 루트 기준 경로
    broadcast_minutes: int = Field(default=30, ge=1)
    voice: str | None = None  # --tts gemini일 때의 보이스. 없으면 엔진 기본값


class SimSpec(_Spec):
    """흉내 값 — Gemini 지연은 2026-11-02 로컬 실측(PROJECT_PLAN 5.1절)의 두 점을 잇는 식이다."""

    tts_chars_per_sec: float = Field(default=7.0, gt=0)  # [임시] 말 속도 — 첫 실제 TTS 회차에서 잰다
    tts_latency_base_sec: float = Field(default=5.5, ge=0)  # 오디오 4초 → 약 7초, 25초 → 약 14초
    tts_latency_per_audio_sec: float = Field(default=0.33, ge=0)
    tts_jitter: float = Field(default=0.2, ge=0, lt=1)  # 지연에 곱하는 흔들림 (±비율)
    # --llm dummy일 때의 LLM 흉내. 지연은 2026-11-07·10 시나리오 실행 실측이다
    # (gpt-6-luna low, 호출 456건: 평균 1.5~1.6초, 표준편차 0.4초). 실제 LLM을 쓰면 무시한다
    llm_latency_sec: float = Field(default=1.5, ge=0)
    llm_jitter: float = Field(default=0.45, ge=0, lt=1)  # ±비율 — 균등분포의 표준편차가 실측과 비슷해진다
    # 채팅 인용을 빠뜨릴 확률. 기본 0은 "항상 인용"이라 반영률이 실제보다 높게 나온다.
    # 같은 실측에서 채팅 인용 38건 중 19건이 빠졌다(문자 낭독 24건은 모두 들어갔다) — 표본이 작아 기본값으로 두지 않았다
    llm_quote_miss_rate: float = Field(default=0.0, ge=0, le=1)
    broadcast_delay_sec: float = Field(default=12.0, ge=0)  # 백엔드 ONAIR_BROADCAST_DELAY_SECONDS
    buffer_low_sec: float = Field(default=15.0, ge=0)  # 백엔드 buffer_low_seconds
    seed: int = 1  # 회차마다 seed + 회차 번호


class TrackSpec(_Spec):
    title: str
    artist: str
    sec: float = Field(default=210.0, gt=0)


DEFAULT_TRACKS = [
    TrackSpec(title="첫눈 오는 밤", artist="가상의 가수", sec=212),
    TrackSpec(title="새벽 버스", artist="모의 밴드", sec=198),
    TrackSpec(title="따뜻한 차 한 잔", artist="시뮬레이션 트리오", sec=236),
    TrackSpec(title="골목길 가로등", artist="가상의 가수", sec=224),
    TrackSpec(title="창가의 오후", artist="테스트 오케스트라", sec=205),
    TrackSpec(title="퇴근길 라디오", artist="모의 밴드", sec=241),
]


class InputSpec(_Spec):
    at: float  # 방송 시작(station.started) 기준 초
    kind: Literal["chat", "message", "song_request"]
    who: str  # 청취자 구분 — 엔진에는 requester_ref로 간다
    body: str = ""  # 신청곡이면 한마디
    nickname: str | None = None
    id: str | None = None  # expect에서 가리킬 이름
    title: str | None = None  # 신청곡
    artist: str | None = None
    sec: float = 210.0

    @field_validator("at", mode="before")
    @classmethod
    def _at(cls, value):
        return parse_at(value)

    @model_validator(mode="after")
    def _check(self) -> InputSpec:
        if self.kind == "song_request" and not (self.title and self.artist):
            raise ValueError("song_request에는 title과 artist가 필요하다")
        if self.kind != "song_request" and not self.body.strip():
            raise ValueError(f"{self.kind}에는 body가 필요하다")
        return self


class ControlSpec(_Spec):
    """운영자 조작 — 테스트 화면의 버튼. 가짜 백엔드가 실제 백엔드처럼 엔진에 알린다 (계약 3.2절)."""

    at: float  # 방송 시작(station.started) 기준 초
    type: Literal["corner_skip"] = "corner_skip"
    corner: str  # 형식 파일의 코너 키 (topic_talk, text_corner, music_intro 등)
    id: str | None = None  # expect에서 가리킬 이름

    @field_validator("at", mode="before")
    @classmethod
    def _at(cls, value):
        return parse_at(value)


class ChatterSpec(_Spec):
    """배경 채팅 — 정해진 줄에서 무작위로 고른다. 도착 간격은 지수분포(평균 60/per_minute초)."""

    per_minute: float = Field(gt=0)
    lines: list[str] = Field(min_length=1)
    people: int = Field(default=5, ge=1)
    start: float = 0.0
    end: float | None = None

    @field_validator("start", "end", mode="before")
    @classmethod
    def _times(cls, value):
        return None if value is None else parse_at(value)


class StorySpec(_Spec):
    body: str
    nickname: str | None = None
    id: str | None = None  # expect에서 가리킬 이름
    at: float | None = None  # 방송 중에 올라온 글의 시각. 없으면 방송 시작 때 후보로 받는다

    @field_validator("at", mode="before")
    @classmethod
    def _at(cls, value):
        return None if value is None else parse_at(value)


class ExpectSpec(_Spec):
    """회차마다 판정하는 기대 결과."""

    type: Literal["reflected", "min_reflected", "aired_contains", "not_aired", "max_silence_sec",
                  "max_fallback", "corner_entered"]
    input: str | None = None  # reflected: inputs 또는 board_stories의 id
    kind: Literal["chat", "message", "song_request", "board_story"] | None = None  # min_reflected
    control: str | None = None  # corner_entered: controls의 id
    # reflected: 도착부터 재생 시작까지, corner_entered: 버튼부터 새 코너 첫 멘트가 들리기까지 (둘 다 방송 지연 포함)
    within_sec: float | None = None
    text: str | None = None  # aired_contains, not_aired
    value: float | None = None  # min_reflected, max_silence_sec, max_fallback
    note: str | None = None  # 사람이 읽을 설명

    @model_validator(mode="after")
    def _check(self) -> ExpectSpec:
        if self.type == "min_reflected" and (self.kind is None or self.value is None):
            raise ValueError("min_reflected에는 kind와 value가 필요하다")
        need = {"reflected": "input", "min_reflected": "kind", "aired_contains": "text", "not_aired": "text",
                "max_silence_sec": "value", "max_fallback": "value",
                "corner_entered": "control"}[self.type]
        if getattr(self, need) is None:
            raise ValueError(f"{self.type}에는 {need}가 필요하다")
        return self


class ScenarioSpec(_Spec):
    name: str
    description: str = ""
    station: StationSpec = Field(default_factory=StationSpec)
    sim: SimSpec = Field(default_factory=SimSpec)
    tracks: list[TrackSpec] = Field(default_factory=lambda: list(DEFAULT_TRACKS), min_length=1)
    board_stories: list[StorySpec] = Field(default_factory=list)
    inputs: list[InputSpec] = Field(default_factory=list)
    controls: list[ControlSpec] = Field(default_factory=list)
    chatter: ChatterSpec | None = None
    expect: list[ExpectSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ids(self) -> ScenarioSpec:
        for i, item in enumerate(self.inputs):
            item.id = item.id or f"i{i}"
        for i, story in enumerate(self.board_stories):
            story.id = story.id or str(i)
        ids = [item.id for item in (*self.inputs, *self.board_stories)]
        if len(set(ids)) != len(ids):
            raise ValueError("inputs·board_stories의 id가 겹친다")
        unknown = [e.input for e in self.expect if e.input is not None and e.input not in ids]
        if unknown:
            raise ValueError(f"expect가 없는 입력을 가리킨다: {unknown}")
        for i, control in enumerate(self.controls):
            control.id = control.id or f"c{i}"
        control_ids = [control.id for control in self.controls]
        if len(set(control_ids)) != len(control_ids):
            raise ValueError("controls의 id가 겹친다")
        unknown = [e.control for e in self.expect if e.control is not None and e.control not in control_ids]
        if unknown:
            raise ValueError(f"expect가 없는 조작을 가리킨다: {unknown}")
        return self

    def request_kinds(self) -> dict[str, str]:
        """엔진에 보내는 request_id → 입력 종류. 배경 채팅은 들어 있지 않다 (runner._inject)."""
        return {**{f"in_{item.id}": item.kind for item in self.inputs},
                **{f"story_sim_{story.id}": "board_story" for story in self.board_stories}}


def load_scenario(path: str | Path) -> ScenarioSpec:
    with open(path, encoding="utf-8") as f:
        return ScenarioSpec.model_validate(yaml.safe_load(f))
