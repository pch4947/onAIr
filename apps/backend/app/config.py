"""Local server configuration; existing environment variables take precedence."""

import os
import math
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    buffer_target_seconds: float = 45
    response_window_seconds: float = 90


def load_settings() -> Settings:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    port = int(os.environ.get("PORT", "3000"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    def positive_seconds(name: str, default: str) -> float:
        value = float(os.environ.get(name, default))
        if not math.isfinite(value) or value < 1:
            raise ValueError(f"{name} must be finite and at least 1")
        return value

    return Settings(host=os.environ.get("HOST", "127.0.0.1"), port=port,
                    buffer_target_seconds=positive_seconds("STREAM_BUFFER_TARGET_SECONDS", "45"),
                    response_window_seconds=positive_seconds("REQUEST_RESPONSE_WINDOW_SECONDS", "90"))
