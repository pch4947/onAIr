"""엔트리포인트 — 설정 로드 후 EngineManager로 로컬 스테이션 기동."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import yaml

from .domain import StationConfig, StationProfile
from .engine import EngineSettings
from .manager import EngineManager


def load_config(path: Path) -> tuple[StationConfig, EngineSettings]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    st = raw["station"]
    config = StationConfig(
        station_id=st["id"],
        profile=StationProfile(**st["profile"]),
        broadcast_minutes=int(st.get("broadcast_minutes", 60)),
        policy_name=raw.get("policy", {}).get("name", "naive_fifo"),
        recent_segments=int(raw.get("context", {}).get("recent_segments", 8)),
    )
    pipe = raw.get("pipeline", {})
    tr = raw.get("transport", {})
    tel = raw.get("telemetry", {})
    settings = EngineSettings(
        transport_kind=tr.get("kind", "stdout"),
        audio_dir=Path(tr.get("audio_dir", "var/audio")),
        sqlite_path=Path(tel.get("sqlite_path", "var/engine_metrics.sqlite")),
        safety_rules_path=Path(pipe.get("safety_rules", "config/safety_rules.yaml")),
        llm=pipe.get("llm", "dummy"),
        tts=pipe.get("tts", "dummy"),
        max_concurrent_generations=int(pipe.get("max_concurrent_generations", 2)),
        target_buffer_sec=float(pipe.get("target_buffer_sec", 30)),
    )
    return config, settings


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="onair-engine", description="onAIr 대본 엔진")
    parser.add_argument("--config", type=Path, default=Path("config/station.example.yaml"))
    parser.add_argument("--max-segments", type=int, default=None,
                        help="N개 제출 후 종료 (관통 테스트용)")
    parser.add_argument("--demo-request", action="append", default=[], metavar="TEXT",
                        help="기동 2초 후 주입할 가짜 청취자 요청 (반복 지정 가능)")
    args = parser.parse_args(argv)

    config, settings = load_config(args.config)
    manager = EngineManager(settings)
    try:
        asyncio.run(manager.run_local(
            config, max_segments=args.max_segments, demo_requests=args.demo_request,
        ))
    except KeyboardInterrupt:
        pass
