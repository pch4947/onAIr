"""채팅 정책 설정 — config/chat_rules.yaml을 검증해 읽는다 (CORNER_DESIGN 5.1.3, 5.7절)."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ChatRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reaction_window_sec: int = Field(ge=1)
    standalone_window_sec: int = Field(ge=1)
    flood_threshold: int = Field(ge=1)
    max_quote_chars: int = Field(ge=1)
    short_reaction_chars: int = Field(ge=1)
    message_max_chars: int = Field(default=200, ge=1)
    pointing: list[str] = Field(default_factory=list)
    reactions: list[str] = Field(default_factory=list)
    steer_min_chars: int = Field(default=6, ge=1)
    steer_questions: list[str] = Field(default_factory=list)
    steer_requests: list[str] = Field(default_factory=list)
    steer_address: list[str] = Field(default_factory=list)
    steer_clarify: list[str] = Field(default_factory=list)
    steer_on_address: bool = True  # DJ를 부르기만 한 채팅(질문·요청이 아닌)도 화제로 삼는다
    steer_points: int = Field(default=2, ge=1, le=4)  # 채팅이 꺼낸 화제로 더 이야기하는 멘트 수
    aggregate_share: float = Field(default=0.4, gt=0, le=1)
    aggregate_min_people: int = Field(default=3, ge=1)
    aggregate_stopwords: list[str] = Field(default_factory=list)
    aggregate_predicate_endings: list[str] = Field(default_factory=list)
    preview_min_messages: int = Field(default=5, ge=1)
    preview_lookahead: int = Field(default=2, ge=0)
    preview_min_remaining_sec: float = Field(default=300.0, ge=0)
    # 청취자가 적은 방송 (CORNER_DESIGN 7.7.1절)
    quiet_after_sec: float = Field(default=180.0, ge=0)
    question_gap_sec: float = Field(default=600.0, ge=0)
    # 곡 앞에서 묻고 곡 뒤에 답을 받는다 (CORNER_DESIGN 7.7.2절)
    question_active_gap_sec: float = Field(default=300.0, ge=0)
    question_open_sec: float = Field(default=300.0, ge=0)
    block_parts: int = Field(default=4, ge=1, le=6)
    block_points: int = Field(default=3, ge=1, le=6)
    song_story_every: int = Field(default=3, ge=0)
    callbacks: bool = True  # 방송 도중에 앞서 나온 청취자 이야기를 다시 꺼낸다. 엔딩 돌아보기는 이 값과 상관없다
    callback_gap_sec: float = Field(default=600.0, ge=0)
    tone_judge: bool = False  # 채팅이 들어올 때 불만·농담인지 LLM에 묻는다 (inputs/chat_tone.py)
    mood_gap_sec: float = Field(default=45.0, ge=0)
    mood_window_sec: float = Field(default=300.0, ge=1)


def load_chat_rules(path: str | Path) -> ChatRules:
    with open(path, encoding="utf-8") as f:
        return ChatRules.model_validate(yaml.safe_load(f))
