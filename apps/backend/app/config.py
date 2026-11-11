"""Local server configuration; existing environment variables take precedence."""

import os
import math
import re
from dataclasses import dataclass, field
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
    fallback_mode: str = "silence"
    buffer_low_seconds: float = 15
    # 다음 멘트가 아직 없을 때 채우는 무음 조각 길이. 짧을수록 멘트가 빨리 이어지지만 조각·경계가 늘어난다
    fill_chunk_seconds: float = 2
    broadcast_delay_seconds: float = 12
    station_id: str = "st_local_dev"
    # 다중 방송 채널 (ONAIR_STATION_IDS). 비어 있으면 station_id 하나만 쓴다
    station_ids: tuple[str, ...] = ()
    audio_dir: Path = Path(__file__).resolve().parents[2] / "engine" / "var" / "audio"
    jwt_secret: str = "dev-insecure-secret-change-me"
    engine_api_url: str = "http://127.0.0.1:8100"
    engine_token: str | None = None
    youtube_max_seconds: int = 600
    lavalink_url: str = 'http://127.0.0.1:2333'
    lavalink_password: str | None = field(default=None, repr=False)
    bgm_audio: Path | None = None
    track_intro_enabled: bool = False
    # 데모용 어드민 참가 핀 → 어드민 닉네임 (ONAIR_ADMIN_PINS). 비어 있으면 어드민 참가를 막는다
    admin_pins: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def channel_ids(self) -> tuple[str, ...]:
        """방송 채널 목록. 첫 번째가 방송 ID 없는 기존 경로가 쓰는 기본 채널이다."""
        return self.station_ids or (self.station_id,)


STATION_ID_PATTERN = r"[A-Za-z0-9_-]{1,64}"


def parse_station_ids(raw: str) -> tuple[str, ...]:
    ids = tuple(filter(None, (part.strip() for part in raw.split(','))))
    if any(not re.fullmatch(STATION_ID_PATTERN, sid) for sid in ids):
        raise ValueError("ONAIR_STATION_IDS must be comma-separated station IDs")
    if 'mine' in ids:  # GET /api/stations/mine
        raise ValueError("ONAIR_STATION_IDS cannot use the reserved ID 'mine'")
    if len(set(ids)) != len(ids):
        raise ValueError("ONAIR_STATION_IDS has a duplicate station ID")
    return ids


def parse_admin_pins(raw: str) -> dict[str, str]:
    """'0808:ADMIN1,0707:ADMIN2' 형식. 핀은 숫자 4~8자리, 닉네임은 영문·숫자 20자 이하."""
    pins: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in raw.split(','))):
        pin, sep, name = item.partition(':')
        if not sep or not re.fullmatch(r'\d{4,8}', pin) or not re.fullmatch(r'[A-Za-z0-9_]{1,20}', name):
            raise ValueError('ONAIR_ADMIN_PINS must look like 0808:ADMIN1,0707:ADMIN2')
        if pin in pins or name in pins.values():
            raise ValueError('ONAIR_ADMIN_PINS has a duplicate pin or name')
        pins[pin] = name
    return pins


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

    def fill_chunk_seconds() -> float:
        value = float(os.environ.get("ONAIR_FILL_CHUNK_SECONDS", "2"))
        if not math.isfinite(value) or not 0.25 <= value <= 4:
            raise ValueError("ONAIR_FILL_CHUNK_SECONDS must be between 0.25 and 4")
        return value

    station_ids = parse_station_ids(os.environ.get("ONAIR_STATION_IDS", ""))
    station_id = station_ids[0] if station_ids else os.environ.get("ONAIR_STATION_ID", "st_local_dev")
    if not re.fullmatch(STATION_ID_PATTERN, station_id):
        raise ValueError("ONAIR_STATION_ID is invalid")
    audio_dir = Path(os.environ.get("ONAIR_AUDIO_DIR", str(Settings.audio_dir))).expanduser()
    if not audio_dir.is_absolute():
        raise ValueError("ONAIR_AUDIO_DIR must be an absolute path")
    fallback_mode = os.environ.get("ONAIR_FALLBACK_MODE", "silence")
    if fallback_mode not in {"music", "silence"}:
        raise ValueError("ONAIR_FALLBACK_MODE must be music or silence")
    fallback = os.environ.get("ONAIR_FALLBACK_AUDIO")
    youtube_max_seconds = int(os.environ.get('ONAIR_YOUTUBE_MAX_SECONDS', '600'))
    if not 1 <= youtube_max_seconds <= 3600:
        raise ValueError('ONAIR_YOUTUBE_MAX_SECONDS must be between 1 and 3600')
    return Settings(host=os.environ.get("HOST", "127.0.0.1"), port=port,
                    fallback_mode=fallback_mode, fallback_audio=Path(fallback).resolve() if fallback else None,
                    buffer_low_seconds=positive_seconds("ONAIR_BUFFER_LOW_SECONDS", "15"),
                    fill_chunk_seconds=fill_chunk_seconds(),
                    broadcast_delay_seconds=positive_seconds("ONAIR_BROADCAST_DELAY_SECONDS", "12"),
                    buffer_target_seconds=positive_seconds("STREAM_BUFFER_TARGET_SECONDS", "45"),
                    response_window_seconds=positive_seconds("REQUEST_RESPONSE_WINDOW_SECONDS", "90"),
                    redis_url=os.environ.get("ONAIR_REDIS_URL") or os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
                    station_id=station_id, station_ids=station_ids, audio_dir=audio_dir.resolve(),
                    hls_enabled=os.environ.get("ONAIR_HLS_ENABLED", "1") == "1",
                    hls_dir=Path(os.environ.get("ONAIR_HLS_DIR", str(Settings.hls_dir))).resolve(),
                    ffmpeg=os.environ.get("ONAIR_FFMPEG", "ffmpeg"),
                    jwt_secret=os.environ.get("ONAIR_JWT_SECRET", Settings.jwt_secret),
                    engine_api_url=os.environ.get("ONAIR_ENGINE_API_URL", Settings.engine_api_url),
                    engine_token=os.environ.get("ONAIR_ENGINE_TOKEN"),
                    youtube_max_seconds=youtube_max_seconds,
                    lavalink_url=os.environ.get('ONAIR_LAVALINK_URL', Settings.lavalink_url).rstrip('/'),
                    lavalink_password=os.environ.get('ONAIR_LAVALINK_PASSWORD'),
                    bgm_audio=Path(os.environ['ONAIR_BGM_AUDIO']).expanduser().resolve()
                    if os.environ.get('ONAIR_BGM_AUDIO') else None,
                    track_intro_enabled=os.environ.get('ONAIR_TRACK_INTRO_ENABLED', '0') == '1',
                    admin_pins=parse_admin_pins(os.environ.get('ONAIR_ADMIN_PINS', '')))
