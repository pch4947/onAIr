"""Local server configuration; existing environment variables take precedence."""

import os
import math
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    buffer_target_seconds: float = 45
    response_window_seconds: float = 90
    redis_url: str = "redis://127.0.0.1:6379/0"
    hls_enabled: bool = False
    hls_dir: Path = Path(__file__).resolve().parents[1] / "var" / "hls"
    ffmpeg: str = "ffmpeg"
    fallback_audio: Path | None = None
    fallback_mode: str = "music"
    buffer_low_seconds: float = 15
    broadcast_delay_seconds: float = 12
    station_id: str = "st_local_dev"
    audio_dir: Path = Path(__file__).resolve().parents[2] / "engine" / "var" / "audio"


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

    station_id = os.environ.get("ONAIR_STATION_ID", "st_local_dev")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", station_id):
        raise ValueError("ONAIR_STATION_ID is invalid")
    audio_dir = Path(os.environ.get("ONAIR_AUDIO_DIR", str(Settings.audio_dir))).expanduser()
    if not audio_dir.is_absolute():
        raise ValueError("ONAIR_AUDIO_DIR must be an absolute path")
    fallback_mode = os.environ.get("ONAIR_FALLBACK_MODE", "music")
    if fallback_mode not in {"music", "silence"}:
        raise ValueError("ONAIR_FALLBACK_MODE must be music or silence")
    fallback = os.environ.get("ONAIR_FALLBACK_AUDIO")
    return Settings(host=os.environ.get("HOST", "127.0.0.1"), port=port,
                    fallback_mode=fallback_mode, fallback_audio=Path(fallback).resolve() if fallback else None,
                    buffer_low_seconds=positive_seconds("ONAIR_BUFFER_LOW_SECONDS", "15"),
                    broadcast_delay_seconds=positive_seconds("ONAIR_BROADCAST_DELAY_SECONDS", "12"),
                    buffer_target_seconds=positive_seconds("STREAM_BUFFER_TARGET_SECONDS", "45"),
                    response_window_seconds=positive_seconds("REQUEST_RESPONSE_WINDOW_SECONDS", "90"),
                    redis_url=os.environ.get("ONAIR_REDIS_URL") or os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                    station_id=station_id, audio_dir=audio_dir.resolve(),
                    hls_enabled=os.environ.get("ONAIR_HLS_ENABLED", "1") == "1",
                    hls_dir=Path(os.environ.get("ONAIR_HLS_DIR", str(Settings.hls_dir))).resolve(),
                    ffmpeg=os.environ.get("ONAIR_FFMPEG", "ffmpeg"))
