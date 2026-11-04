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


def load_chat_rules(path: str | Path) -> ChatRules:
    with open(path, encoding="utf-8") as f:
        return ChatRules.model_validate(yaml.safe_load(f))
