"""apps/engine/.env 로딩 — 백엔드와 같은 규칙 (OS 환경변수 우선)."""
from __future__ import annotations

import os

from onair_engine.main import ENV_FILE, load_env


def test_env_file_is_engine_root():
    assert ENV_FILE.name == ".env"
    assert (ENV_FILE.parent / "pyproject.toml").is_file()


def test_load_env_reads_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_TTS_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("GOOGLE_TTS_API_KEY=from-file\n", encoding="utf-8")

    assert load_env(env) is True
    assert os.environ["GOOGLE_TTS_API_KEY"] == "from-file"
    monkeypatch.delenv("GOOGLE_TTS_API_KEY")  # 다른 테스트로 새지 않게


def test_os_environment_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "from-os")
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=from-file\n", encoding="utf-8")

    load_env(env)
    assert os.environ["OPENAI_API_KEY"] == "from-os"


def test_missing_file_is_fine(tmp_path):
    assert load_env(tmp_path / "없음.env") is False
