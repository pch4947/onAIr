"""실행 결과 비교 (onair-compare) — 설정값이나 코드를 바꾼 것이 지표를 움직였는지 판정한다.

  onair-compare DIR             한 실행의 회차 간 편차. 무엇이 노이즈인지 먼저 잰다
  onair-compare BASE CAND ...   기준 대비 후보마다 표. 수정 전·후 실행, 또는 --sweep의 변형 폴더

같은 회차 번호는 같은 seed다(입력 도착 시각이 같다). 그래서 회차끼리 짝지어 차이를 보고,
그 차이의 평균이 회차 간 흔들림을 넘을 때만 개선·악화로 적는다 (짝 t 검정, 양쪽 95%).
지표가 많으면 우연히 넘는 것이 섞인다 — 실행 전에 정한 목표 지표로 판정하고 나머지는 부작용 확인에 쓴다
(docs/ENGINE_TUNING.md 4절).
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

from .metrics import HIGH, LOW, METRICS, Metric, spread

MIN_PAIRS = 3  # 이보다 적으면 판정하지 않는다
# 양쪽 95% t 임계값 — 자유도 1~30. 그 위는 정규 근사
_T95 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145,
        2.131, 2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048,
        2.045, 2.042)
IMPROVED, WORSE, SAME, FEW, NOTE = "개선", "악화", "차이 없음", "표본 부족", "참고"


def t95(df: int) -> float:
    return _T95[df - 1] if df <= len(_T95) else 1.96


def _json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load(root: Path) -> dict[str, dict[int, dict]]:
    """실행 폴더(<시각> 또는 <시각>/<변형>) → {시나리오: {회차 번호: {report, seed, error}}}."""
    out: dict[str, dict[int, dict]] = {}
    for run_dir in sorted(Path(root).glob("*/run-*")):
        report = _json(run_dir / "report.json")
        if report is None:
            continue
        try:
            index = int(run_dir.name.split("-", 1)[1])
        except ValueError:
            continue
        meta = _json(run_dir / "run.json") or {}
        out.setdefault(run_dir.parent.name, {})[index] = {
            "report": report, "seed": meta.get("seed"), "meta": meta,
            "error": (report.get("sim") or {}).get("error"),
        }
    return out


def expand(roots: list[Path]) -> list[Path]:
    """--sweep 실행 폴더 하나를 넘기면 기준(base)과 변형 폴더들로 펼친다."""
    if len(roots) != 1:
        return roots
    session = _json(roots[0] / "session.json") or {}
    names = [name for name in session.get("variants") or {} if (roots[0] / name).is_dir()]
    return [roots[0] / name for name in names] if len(names) > 1 else roots


def describe(root: Path) -> str:
    """표 머리에 적는 출처 한 줄 — 폴더, 커밋, LLM, 덮어쓴 값. 보고서에 그대로 옮길 수 있게 쓴다."""
    root = Path(root)
    session = _json(root / "session.json") or _json(root.parent / "session.json") or {}
    first = next(iter(sorted(root.glob("*/run-*/run.json"))), None)
    meta = (_json(first) if first else None) or {}
    git = meta.get("git") or session.get("git") or "?"
    dirty = meta.get("git_dirty", session.get("git_dirty"))
    llm = meta.get("llm") or session.get("llm") or {}
    parts = [f"`{root.parent.name}/{root.name}`" if (root.parent / "session.json").exists() else f"`{root.name}`",
             f"커밋 {git}{' + 커밋 안 한 변경' if dirty else ''}",
             f"LLM {llm.get('kind', '?')} {llm.get('model') or ''}".rstrip(), f"TTS {meta.get('tts', '?')}"]
    if overrides := meta.get("overrides"):
        parts.append("설정 " + ", ".join(f"{k}={v}" for k, v in overrides.items()))
    return " · ".join(parts)


def _values(runs: dict[int, dict], metric: Metric) -> dict[int, float]:
    out = {}
    for index, run in runs.items():
        if run["error"]:
            continue  # 오류로 끝난 회차의 지표는 믿을 수 없다
        value = metric.get(run["report"])
        if value is not None:
            out[index] = value
    return out


def _verdict(metric: Metric, diffs: list[float]) -> tuple[str, float | None]:
    """(판정, 표준오차). 판정은 차이의 평균이 0과 구분되는지만 본다."""
    n = len(diffs)
    se = statistics.stdev(diffs) / math.sqrt(n) if n > 1 else None
    if metric.better is None:
        return NOTE, se
    if n < MIN_PAIRS:
        return FEW, se
    diff = statistics.fmean(diffs)
    if abs(diff) < 1e-12 or (se and abs(diff) / se <= t95(n - 1)):
        return SAME, se
    return (IMPROVED if (diff < 0) == (metric.better == LOW) else WORSE), se


def compare(base: dict[str, dict[int, dict]], cand: dict[str, dict[int, dict]]) -> dict[str, dict]:
    """시나리오마다 지표별 짝 비교. 양쪽에 다 있는 회차 번호만 쓴다."""
    out: dict[str, dict] = {}
    for scenario, base_runs in base.items():
        if scenario not in cand:
            continue
        cand_runs = cand[scenario]
        shared = sorted(set(base_runs) & set(cand_runs))
        ok = [i for i in shared if not base_runs[i]["error"] and not cand_runs[i]["error"]]
        rows = []
        for metric in METRICS:
            b, c = _values(base_runs, metric), _values(cand_runs, metric)
            paired = [i for i in ok if i in b and i in c]
            if not paired:
                continue
            diffs = [c[i] - b[i] for i in paired]
            verdict, se = _verdict(metric, diffs)
            sign = -1 if metric.better == LOW else 1
            rows.append({
                "key": metric.key, "label": metric.label, "better": metric.better, "n": len(paired),
                "base": spread([b[i] for i in paired]), "cand": spread([c[i] for i in paired]),
                "diff": round(statistics.fmean(diffs), 4), "se": round(se, 4) if se is not None else None,
                "wins": sum(1 for d in diffs if d * sign > 1e-12) if metric.better else None,
                "losses": sum(1 for d in diffs if d * sign < -1e-12) if metric.better else None,
                "verdict": verdict,
            })
        out[scenario] = {
            "pairs": len(ok), "skipped_errors": len(shared) - len(ok),
            "same_seeds": all(base_runs[i]["seed"] == cand_runs[i]["seed"] for i in ok),
            "rows": rows,
        }
    return out


def _num(value: float | None) -> str:
    if value is None:
        return "-"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}" if abs(value) >= 100 else f"{value:.2f}"


def _mean_sd(stats: dict) -> str:
    return _num(stats["mean"]) + (f" ± {_num(stats['sd'])}" if stats["sd"] else "")


def _arrow(better: str | None) -> str:
    return {LOW: " ↓", HIGH: " ↑"}.get(better, "")


def render_compare(base_root: Path, cand_root: Path, result: dict[str, dict], *,
                   base_label: str | None = None, cand_label: str | None = None, heading: str = "#") -> str:
    base_label, cand_label = base_label or Path(base_root).name, cand_label or Path(cand_root).name
    moved = {IMPROVED: [], WORSE: []}
    for scenario, block in result.items():
        for row in block["rows"]:
            if row["verdict"] in moved:
                moved[row["verdict"]].append(f"{scenario} {row['label']}({row['diff']:+.2f})")
    lines = [f"{heading} 비교 · {base_label} → {cand_label}", "",
             f"- 기준: {describe(base_root)}", f"- 후보: {describe(cand_root)}",
             f"- 개선: {', '.join(moved[IMPROVED]) or '없음'}", f"- 악화: {', '.join(moved[WORSE]) or '없음'}", ""]
    if not result:
        lines += ["겹치는 시나리오가 없습니다.", ""]
    for scenario, block in result.items():
        notes = []
        if block["skipped_errors"]:
            notes.append(f"오류 회차 {block['skipped_errors']}쌍 제외")
        if not block["same_seeds"]:
            notes.append("**seed가 달라 짝 비교가 아니다 — 판정은 참고만**")
        lines += [f"{heading}# {scenario} · {block['pairs']}쌍" + (f" · {' · '.join(notes)}" if notes else ""), "",
                  "| 지표 | 기준 | 후보 | 차이 | 좋아짐/나빠짐 | 판정 |", "|---|---|---|---|---|---|"]
        flat = []  # 양쪽 모든 회차가 같은 값 — 표를 채우지 않고 한 줄로 모은다
        for row in block["rows"]:
            if not row["diff"] and not row["base"]["sd"] and not row["cand"]["sd"]:
                flat.append(f"{row['label']} {_num(row['base']['mean'])}")
                continue
            record = f"{row['wins']}/{row['losses']}" if row["wins"] is not None else "-"
            verdict = f"**{row['verdict']}**" if row["verdict"] in moved else row["verdict"]
            lines.append(f"| {row['label']}{_arrow(row['better'])} | {_mean_sd(row['base'])} | "
                         f"{_mean_sd(row['cand'])} | {row['diff']:+.2f} | {record} | {verdict} |")
        lines += ["", f"모든 회차에서 양쪽이 같은 지표: {', '.join(flat) or '없음'}", ""]
    footnote = ("값은 평균 ± 표준편차, ↑↓는 좋아지는 방향이다. 판정은 같은 seed 회차끼리의 차이가 "
                f"회차 간 흔들림을 넘는지 본다(짝 t 검정, 양쪽 95%, {MIN_PAIRS}쌍 이상). "
                "방향이 없는 지표는 참고로만 적는다.")
    return "\n".join([*lines, footnote, ""])


def render_noise(root: Path, runs: dict[str, dict[int, dict]]) -> str:
    """한 실행의 회차 간 편차 — 같은 조건을 되풀이했을 때 지표가 얼마나 흔들리는지."""
    lines = [f"# 회차 간 편차 · {Path(root).name}", "", f"- 출처: {describe(root)}", ""]
    for scenario, by_index in runs.items():
        errors = sum(1 for run in by_index.values() if run["error"])
        lines += [f"## {scenario} · {len(by_index) - errors}회" + (f" · 오류 {errors}회 제외" if errors else ""), "",
                  "| 지표 | 회차 | 평균 | 표준편차 | 최소 | 최대 | 구분되는 차이 |", "|---|---|---|---|---|---|---|"]
        for metric in METRICS:
            stats = spread(list(_values(by_index, metric).values()))
            if stats is None:
                continue
            n, sd = stats["n"], stats["sd"]
            # 같은 회차 수의 두 실행을 견줄 때 이만큼은 달라져야 흔들림과 구분된다 (짝 없는 t 검정 기준)
            detectable = _num(t95(2 * n - 2) * sd * math.sqrt(2 / n)) if sd is not None else "-"
            lines.append(f"| {metric.label}{_arrow(metric.better)} | {n} | {_num(stats['mean'])} | "
                         f"{_num(sd)} | {_num(stats['min'])} | {_num(stats['max'])} | {detectable} |")
        lines.append("")
    footnote = ("구분되는 차이: 같은 회차 수로 두 조건을 견줄 때 평균이 이만큼은 달라져야 우연과 구분된다"
                "(양쪽 95%). 이 값이 기대하는 개선 폭보다 크면 회차를 늘린다.")
    return "\n".join([*lines, footnote, ""])


def write_variants(out: Path, names: list[str]) -> Path | None:
    """--sweep 실행이 끝나면 부른다 — 첫 변형(base) 대비 나머지의 비교를 out/compare.md·json으로 남긴다."""
    if len(names) < 2:
        return None
    base = load(out / names[0])
    sections, data = [], {}
    for name in names[1:]:
        data[name] = compare(base, load(out / name))
        sections.append(render_compare(out / names[0], out / name, data[name], heading="##"))
    (out / "compare.json").write_text(json.dumps({"base": names[0], "variants": data}, ensure_ascii=False,
                                                 indent=2), encoding="utf-8")
    path = out / "compare.md"
    path.write_text(f"# 설정값 비교 · {out.name}\n\n" + "\n".join(sections), encoding="utf-8")
    return path


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="onair-compare", description="시나리오 실행 결과 비교 — 폴더 하나면 회차 간 편차, 둘 이상이면 첫 폴더 대비")
    parser.add_argument("roots", nargs="+", type=Path,
                        help="실행 결과 폴더 (var/scenario/<시각> 또는 그 안의 변형 폴더). 첫 폴더가 기준이다")
    parser.add_argument("--label", action="append", default=None, help="폴더마다 붙일 이름 (순서대로)")
    parser.add_argument("-o", "--out", type=Path, default=None, help="마크다운을 파일로도 쓴다")
    parser.add_argument("--json", type=Path, default=None, help="원자료를 JSON으로 쓴다")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    roots = expand(args.roots)
    missing = [str(r) for r in roots if not r.is_dir()]
    if missing:
        print(f"폴더가 없습니다: {', '.join(missing)}", file=sys.stderr)
        return 1
    loaded = [load(r) for r in roots]
    if not loaded[0]:
        print(f"회차가 없습니다 (<시나리오>/run-N/report.json): {roots[0]}", file=sys.stderr)
        return 1
    labels = list(args.label or []) + [None] * len(roots)
    if len(roots) == 1:
        text, data = render_noise(roots[0], loaded[0]), None
    else:
        data = {str(roots[i]): compare(loaded[0], loaded[i]) for i in range(1, len(roots))}
        text = "\n".join(render_compare(roots[0], roots[i], data[str(roots[i])],
                                        base_label=labels[0], cand_label=labels[i])
                         for i in range(1, len(roots)))
    print(text, end="")
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    if args.json and data is not None:
        args.json.write_text(json.dumps({"base": str(roots[0]), "candidates": data}, ensure_ascii=False,
                                        indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
