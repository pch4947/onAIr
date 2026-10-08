"""엔트리포인트 — 설정 로드 후 EngineManager로 로컬 스테이션 기동."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .domain import StationConfig, StationProfile
from .engine import EngineSettings
from .manager import EngineManager
from .pipeline.tts import list_cartesia_voices

# apps/engine/.env — 백엔드(apps/backend/.env)와 같은 방식. 키는 설정 파일이 아니라 여기에 둔다
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def load_env(path: Path = ENV_FILE) -> bool:
    """.env를 환경변수로 읽는다. 이미 설정된 OS 환경변수가 우선한다 (백엔드와 동일)."""
    return load_dotenv(path, override=False)


# persona 파일에서 프로필 필드가 아닌 메타데이터 — 읽고 버린다
_PERSONA_META = {"persona_id", "description"}


def load_profile(spec: dict | str | Path) -> StationProfile:
    """station.profile — 필드를 직접 쓰거나 persona 파일(config/personas/*.yaml) 경로를 준다."""
    if not isinstance(spec, dict):
        spec = yaml.safe_load(Path(spec).read_text(encoding="utf-8"))
    return StationProfile(**{k: v for k, v in spec.items() if k not in _PERSONA_META})


def load_config(path: Path) -> tuple[StationConfig, EngineSettings]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    st = raw["station"]
    config = StationConfig(
        station_id=st["id"],
        profile=load_profile(st["profile"]),
        broadcast_minutes=int(st.get("broadcast_minutes", 60)),
        policy_name=raw.get("policy", {}).get("name", "naive_fifo"),
        recent_segments=int(raw.get("context", {}).get("recent_segments", 8)),
    )
    pipe = raw.get("pipeline", {})
    tr = raw.get("transport", {})
    tel = raw.get("telemetry", {})
    settings = EngineSettings(
        transport_kind=tr.get("kind", "stdout"),
        transport_base_url=tr.get("base_url", "http://localhost:3000"),
        # 토큰·비밀번호는 커밋되는 설정 파일이 아니라 환경변수로 받는다
        transport_token=os.environ.get("ONAIR_ENGINE_TOKEN") or tr.get("token"),
        transport_redis_url=(os.environ.get("ONAIR_REDIS_URL")
                             or tr.get("redis_url", "redis://localhost:6379/0")),
        audio_dir=Path(tr.get("audio_dir", "var/audio")),
        sqlite_path=Path(tel.get("sqlite_path", "var/engine_metrics.sqlite")),
        safety_rules_path=Path(pipe.get("safety_rules", "config/safety_rules.yaml")),
        llm=pipe.get("llm", "dummy"),
        llm_model=pipe.get("llm_model"),
        llm_base_url=pipe.get("llm_base_url"),
        llm_reasoning_effort=pipe.get("llm_reasoning_effort"),
        tts=pipe.get("tts", "dummy"),
        tts_voice=pipe.get("tts_voice"),
        tts_cache_dir=Path(pipe["tts_cache_dir"]) if pipe.get("tts_cache_dir") else None,
        max_concurrent_generations=int(pipe.get("max_concurrent_generations", 2)),
        target_buffer_sec=float(pipe.get("target_buffer_sec", 30)),
    )
    return config, settings


def add_llm_args(parser: argparse.ArgumentParser) -> None:
    """LLM 선택 옵션 — onair-engine과 onair-eval이 같이 쓴다."""
    parser.add_argument("--llm", choices=["dummy", "claude", "gemini", "openai"], default=None,
                        help="설정 파일의 pipeline.llm을 덮어쓴다")
    parser.add_argument("--llm-model", default=None, metavar="MODEL",
                        help="설정 파일의 pipeline.llm_model을 덮어쓴다")
    parser.add_argument("--llm-base-url", default=None, metavar="URL",
                        help="openai 호환 서버 주소 (Qwen DashScope, Ollama 등)")
    parser.add_argument("--llm-reasoning-effort", default=None, metavar="EFFORT",
                        help="설정 파일의 pipeline.llm_reasoning_effort를 덮어쓴다 (추론 모델용, 예: low)")


def apply_llm_args(settings: EngineSettings, args: argparse.Namespace) -> None:
    if args.llm:
        settings.llm = args.llm
    if args.llm_model:
        settings.llm_model = args.llm_model
    if args.llm_base_url:
        settings.llm_base_url = args.llm_base_url
    if args.llm_reasoning_effort:
        settings.llm_reasoning_effort = args.llm_reasoning_effort


def cli(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="onair-engine", description="onAIr 대본 엔진")
    parser.add_argument("--config", type=Path, default=Path("config/station.example.yaml"))
    parser.add_argument("--max-segments", type=int, default=None,
                        help="N개 제출 후 종료 (관통 테스트용)")
    parser.add_argument("--demo-request", action="append", default=[], metavar="TEXT",
                        help="기동 2초 후 주입할 가짜 청취자 요청 (반복 지정 가능)")
    parser.add_argument("--persona", type=Path, default=None, metavar="FILE",
                        help="설정 파일의 station.profile을 persona 파일로 덮어쓴다")
    add_llm_args(parser)
    parser.add_argument("--tts", choices=["dummy", "google", "edge", "cartesia"], default=None,
                        help="설정 파일의 pipeline.tts를 덮어쓴다")
    parser.add_argument("--list-voices", action="store_true",
                        help="Cartesia 한국어 보이스 목록(ID·이름·성별)을 출력하고 종료 — pipeline.tts_voice에 쓸 ID")
    parser.add_argument("--transport", choices=["stdout", "http", "redis"], default=None,
                        help="설정 파일의 transport.kind 덮어쓰기 (관통 테스트용)")
    parser.add_argument("--backend-url", default=None, metavar="URL",
                        help="설정 파일의 transport.base_url 덮어쓰기")
    args = parser.parse_args(argv)
    # load_config가 ONAIR_REDIS_URL 등을 읽으므로 그보다 먼저 불러온다
    load_env()
    if args.list_voices:
        for v in list_cartesia_voices():
            print(f"{v['id']}	{v['name']}	{v['gender'] or '-'}")
        return

    # 생성된 대본을 SCRIPT 라인으로 보여준다 (SUBMIT 페이로드에는 대본 텍스트가 없다)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config, settings = load_config(args.config)
    if args.persona:
        config.profile = load_profile(args.persona)
    apply_llm_args(settings, args)
    if args.tts:
        settings.tts = args.tts
    if args.transport:
        settings.transport_kind = args.transport
    if args.backend_url:
        settings.transport_base_url = args.backend_url
    manager = EngineManager(settings)
    try:
        asyncio.run(manager.run_local(
            config, max_segments=args.max_segments, demo_requests=args.demo_request,
        ))
    except KeyboardInterrupt:
        pass
