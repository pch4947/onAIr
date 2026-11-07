"""대본 검토 페이지 — 시나리오 실행 결과를 사람이 읽고 표시하는 관리자용 HTML 한 장 (#229).

실행기는 형식(무음, 반영, 기대 결과)만 판정한다. 같은 말 반복, 흐름 단절, 청취자 글 오해처럼 읽어야 아는
부자연스러움은 사람이 잡는다. 이 페이지는 그 작업을 돕는다.
- 회차 대본을 시각순으로 보여 주고, 멘트마다 분류와 메모를 단다 (브라우저 localStorage에 저장)
- 반복 의심(같은 첫머리, 비슷한 멘트, 질문 연속, 주제 다시 소개)은 자동 표시로 먼저 짚는다
- 두 회차를 나란히, 어느 쪽인지 숨기고(블라인드) 비교해 더 자연스러운 쪽을 고른다
- 표시한 것은 issues.csv 형식으로 내보낸다

출력 폴더 여러 개(예: 수정 전·후 실행)를 한 페이지에 모을 수 있다. 서버 없이 파일로 연다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

TEMPLATE = Path(__file__).with_name("review.html")
_DATA_SLOT = "/*__REVIEW_DATA__*/null"

# transcript.md 한 줄 — played.json이 없는 예전 회차용 (runner._transcript 형식)
_HEAD = re.compile(r"^- `(\d+):(\d\d)` (.*)$")
_SPEECH = re.compile(r"^\*\*\[(?P<corner>[^\]]+)\]\*\* \((?P<sec>\d+)초\)(?: · 반영: (?P<refl>.+?))?\s*$")
_MUSIC = re.compile(r"^♪ 곡 · (?P<text>.*) \((?P<m>\d+):(?P<s>\d\d)\)$")
_SILENCE = re.compile(r"^⏸ \*\*무음 (?P<sec>[\d.]+)초\*\*$")
_INPUT = re.compile(r"^▶ 입력 · (?P<text>.*)$")
_GLUE_TEXT = "(고정 문구 또는 엔진 종료 뒤 송출)"


def parse_transcript(text: str) -> list[dict]:
    """transcript.md → played 목록. 코너는 방송 이름(예: 오늘의 이야기)으로만 남는다."""
    played: list[dict] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not (m := _HEAD.match(line)):
            continue
        at, rest = int(m[1]) * 60 + int(m[2]), m[3]
        if s := _SPEECH.match(rest):
            body = lines[i + 1].strip() if i + 1 < len(lines) else ""
            played.append({"at": at, "kind": "speech", "sec": int(s["sec"]), "corner": s["corner"],
                           "script": None if body == _GLUE_TEXT else body,
                           "reflected": s["refl"].split(", ") if s["refl"] else []})
        elif s := _MUSIC.match(rest):
            played.append({"at": at, "kind": "music", "sec": int(s["m"]) * 60 + int(s["s"]), "text": s["text"]})
        elif s := _SILENCE.match(rest):
            played.append({"at": at, "kind": "silence", "sec": float(s["sec"])})
        elif s := _INPUT.match(rest):
            played.append({"at": at, "kind": "input", "sec": 0, "text": s["text"]})
    return played


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _summary(report: dict | None) -> dict:
    """사이드바에 보일 핵심 지표. 예전 회차에 없는 값은 None이다."""
    report = report or {}
    sim, gen, ref = report.get("sim", {}), report.get("generation", {}), report.get("reflection", {})
    expect = report.get("expect") or []
    return {
        "talk_ratio": sim.get("talk_ratio"),
        "talk_between_songs_p50": sim.get("talk_between_songs_p50"),
        "back_to_back_songs": sim.get("back_to_back_songs"),
        "silence_total_sec": sim.get("silence_total_sec"),
        "fallback": (gen.get("outcomes") or {}).get("fallback"),
        "message_reflection_rate": ref.get("message_reflection_rate"),
        "standalone_reflection_rate": ref.get("standalone_reflection_rate"),
        "expect": f"{sum(e['passed'] for e in expect)}/{len(expect)}" if expect else None,
        "topic": report.get("topic"),
    }


def collect(root: Path, source: str | None = None) -> list[dict]:
    """출력 폴더 하나의 회차 전부. root는 실행 시각 폴더(<out>/<시각>) 또는 그 안의 시나리오 폴더."""
    root = Path(root)
    runs = sorted(root.glob("*/run-*")) or sorted(root.glob("run-*"))  # 시나리오 폴더를 바로 넘겨도 된다
    source = source or root.name
    out = []
    for run_dir in runs:
        if not run_dir.is_dir():
            continue
        saved = _load_json(run_dir / "played.json")
        if saved is not None:
            played = saved["played"]
        elif (run_dir / "transcript.md").exists():
            played = parse_transcript((run_dir / "transcript.md").read_text(encoding="utf-8"))
        else:
            continue
        meta = _load_json(run_dir / "run.json") or {}
        out.append({
            "source": source,
            "scenario": run_dir.parent.name,
            "run": run_dir.name,
            "git": meta.get("git"),
            "llm": (meta.get("llm") or {}).get("model"),
            "tts": meta.get("tts"),
            "description": (meta.get("scenario") or {}).get("description"),
            "summary": _summary(_load_json(run_dir / "report.json")),
            "played": played,
        })
    return out


def render(runs: list[dict]) -> str:
    # </script>로 블록이 닫히지 않게 한다 — 대본은 LLM 출력이다
    data = json.dumps({"runs": runs}, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace(_DATA_SLOT, data)


def write(roots: list[Path], out: Path, labels: list[str] | None = None) -> int:
    runs = []
    for i, root in enumerate(roots):
        runs += collect(root, labels[i] if labels and i < len(labels) else None)
    out.write_text(render(runs), encoding="utf-8")
    return len(runs)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="onair-review", description="시나리오 실행 결과의 대본 검토 페이지(HTML 한 장)를 만든다")
    parser.add_argument("roots", nargs="+", type=Path,
                        help="실행 결과 폴더 (var/scenario/<시각>). 여러 개면 한 페이지에 모은다 — 수정 전·후 비교")
    parser.add_argument("--label", action="append", default=None,
                        help="폴더마다 붙일 이름 (순서대로, 예: --label 수정전 --label 수정후)")
    parser.add_argument("-o", "--out", type=Path, default=None, help="기본: 첫 폴더/review.html")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    missing = [str(r) for r in args.roots if not r.is_dir()]
    if missing:
        print(f"폴더가 없습니다: {', '.join(missing)}", file=sys.stderr)
        return 1
    out = args.out or args.roots[0] / "review.html"
    count = write(args.roots, out, args.label)
    if not count:
        print("회차가 없습니다 (run-N/transcript.md 또는 played.json)", file=sys.stderr)
        return 1
    print(f"회차 {count}개 → {out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
