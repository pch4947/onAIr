"""config/formats/*.yaml 로드·검증.

common.yaml에 형식 공통 코너(오프닝·엔딩·곡 소개 등)와 참여 강도 프리셋을 두고,
형식 파일(music.yaml 등)이 자기 코너를 더하거나 같은 이름으로 덮어쓴다.
"""
from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

from ..corners.steps import STEP_KINDS
from .spec import FormatSpec

FORMATS = ("music", "talk", "news")
DEFAULT_FORMATS_DIR = Path("config/formats")


def load_format(name: str, formats_dir: Path | str = DEFAULT_FORMATS_DIR) -> FormatSpec:
    """형식 정의를 읽어 검증한다. 같은 파일은 한 번만 읽는다 — 방마다 다시 파싱하지 않는다."""
    if name not in FORMATS:
        raise ValueError(f"unknown format: {name} (available: {list(FORMATS)})")
    return _load(name, str(Path(formats_dir).resolve()))


@cache
def _load(name: str, formats_dir: str) -> FormatSpec:
    root = Path(formats_dir)
    common = _read(root / "common.yaml")
    own = _read(root / f"{name}.yaml")
    corners = {**common.get("corners", {}), **own.pop("corners", {})}
    for key, corner in corners.items():
        corner["corner_type"] = key
    intensity = {**common.get("intensity", {}), **own.pop("intensity", {})}
    # 공통 코너 중 이 형식이 쓰지 않는 것은 뺀다 — 접착 문구를 합성하지 않고, 계약 검사에도 안 걸리게
    corners = {k: v for k, v in corners.items() if k in _reachable(own, corners)}
    fmt = FormatSpec.model_validate({**own, "corners": corners, "intensity": intensity})
    unknown = sorted({f"{c.corner_type}.{s.kind}" for c in fmt.corners.values() for s in c.steps
                      if s.kind != "glue" and s.kind not in STEP_KINDS})
    if unknown:
        raise ValueError(f"{name}: 알 수 없는 단계 유형 {unknown} (corners/steps.py STEP_KINDS)")
    return fmt


def _reachable(own: dict, corners: dict) -> set[str]:
    """형식 파일이 가리키는 코너 — 오프닝·엔딩·순환·호스트 후보·대체 코너(연쇄)와 filler."""
    names = {own.get("opening", "opening"), own.get("closing", "closing"), "filler",
             *own.get("host_corners", []), *own.get("default_main", [])}
    for entry in own.get("cycle", []):
        if entry.startswith("$section:"):
            names.add(entry.split(":", 1)[1])
        elif not entry.startswith("$"):
            names.add(entry)
    todo = list(names)
    while todo:
        fallback = (corners.get(todo.pop()) or {}).get("fallback")
        if fallback and fallback not in names:
            names.add(fallback)
            todo.append(fallback)
    return names


def _read(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise TypeError(f"{path}: 최상위가 매핑이어야 한다")
    return data
