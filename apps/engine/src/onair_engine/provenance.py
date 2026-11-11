"""재현 정보 — 실행 결과가 어떤 코드에서 나왔는지 남긴다 (튜닝 기록과 보고서 근거, docs/ENGINE_TUNING.md).

커밋 번호만으로는 부족하다. 프롬프트를 고쳐 보는 동안에는 커밋하지 않은 채로 돌리므로,
추적 중인 파일의 변경(diff)도 함께 남겨야 그 결과를 다시 만들 수 있다.
"""
from __future__ import annotations

import functools
import subprocess
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[2]  # apps/engine


def _git(*args: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=ENGINE_ROOT, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


@functools.cache
def git_diff() -> str:
    """커밋하지 않은 엔진 변경 — 추적 중인 파일만. 실행을 시작할 때의 상태를 한 번만 읽는다."""
    return _git("diff", "HEAD", "--", ".") or ""


@functools.cache
def snapshot() -> dict:
    """run.json·session.json에 넣는 값. git_dirty가 참이면 git_diff()를 같이 남겨야 재현된다."""
    commit = _git("rev-parse", "--short", "HEAD")
    return {"git": (commit or "").strip() or None, "git_dirty": bool(git_diff().strip())}


def write_diff(out_dir: Path) -> Path | None:
    """커밋하지 않은 변경이 있으면 out_dir/worktree.diff로 남긴다."""
    if not git_diff().strip():
        return None
    path = out_dir / "worktree.diff"
    path.write_text(git_diff(), encoding="utf-8")
    return path
