"""방송 1회의 정량 지표 — 계측 SQLite를 읽기 전용으로 집계한다 (CORNER_DESIGN 9.5절, 2026-11-07 회의 7번).

방송 동작에는 관여하지 않는다. 같은 DB에 여러 방송이 쌓이므로 station_id로 고른다.
비율은 분모가 0이면 None, 시간은 초 단위다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .telemetry import _read

Q_MAX_SEC = 120.0  # 제때 반영 기준 — telemetry.reflection_summary와 같은 값 (CORNER_DESIGN 9.5절)
_MATERIALS = {"chat": "채팅", "callback": "다시 꺼내기", "angle": "갈래", "topic": "주제만", "block": "덩어리 문단"}
OPENING_CHARS = 5  # 첫머리 — 공백·문장부호를 뺀 앞 글자 수. "겨울 간식은 …"으로 계속 시작하는지 본다
SIMILAR_WINDOW = 8  # 유사도를 견줄 직전 멘트 수 — 파이프라인이 LLM에 주는 직전 대본 수와 같다
_NOT_WORD = re.compile(r"[\s\W_]+")


def _normalize(text: str) -> str:
    return _NOT_WORD.sub("", text)


def _trigrams(text: str) -> set[str]:
    t = _normalize(text)
    return {t[i:i + 3] for i in range(len(t) - 2)}


def script_stats(scripts: list[str], topic: str | None) -> dict:
    """송출된 대본의 반복 지표 (#222) — 같은 말을 되풀이하는지, 주제를 처음부터 다시 꺼내는지.

    - same_opening_rate: 첫머리(OPENING_CHARS)가 앞선 멘트와 같은 멘트의 비율
    - similarity_mean: 멘트마다 직전 SIMILAR_WINDOW개 중 가장 비슷한 것과의 글자 3-gram 자카드 유사도 평균
    - topic_intros: "오늘은 ○○ 이야기를 …"처럼 주제를 소개하는 멘트 수. 오프닝의 1번이 정상이다
    - question_end_rate: 질문으로 끝난 멘트의 비율. 답이 없는 방송에서 높으면 계속 묻기만 하는 것이다 (#262)
    """
    if not scripts:
        return {"segments": 0, "same_opening_rate": None, "similarity_mean": None, "topic_intros": None,
                "question_end_rate": None}
    openings: set[str] = set()
    same = 0
    similarities: list[float] = []
    grams = [_trigrams(s) for s in scripts]
    for i, script in enumerate(scripts):
        head = _normalize(script)[:OPENING_CHARS]
        same += head in openings
        openings.add(head)
        previous = grams[max(0, i - SIMILAR_WINDOW):i]
        if previous and grams[i]:
            similarities.append(max(len(grams[i] & p) / len(grams[i] | p) for p in previous))
    intros = None
    if topic and _normalize(topic):
        name = r"\s*".join(map(re.escape, _normalize(topic)))  # 띄어쓰기가 달라도 같은 주제다
        intro = re.compile(rf"(오늘|이번)[^.?!]{{0,15}}{name}[^.?!]{{0,15}}(이야기|얘기)")
        intros = sum(1 for s in scripts if intro.search(s))
    questions = sum(1 for s in scripts if s.rstrip().endswith(("?", "？")))
    return {"segments": len(scripts), "same_opening_rate": _ratio(same, len(scripts)),
            "similarity_mean": round(sum(similarities) / len(similarities), 3) if similarities else None,
            "topic_intros": intros, "question_end_rate": _ratio(questions, len(scripts))}


def _percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, -(-pct * len(ordered) // 100) - 1)  # nearest-rank — telemetry의 P95와 같은 방식
    return round(ordered[rank], 3)


def _spread(values: list[float]) -> dict:
    return {"count": len(values), "p50": _percentile(values, 50), "p95": _percentile(values, 95)}


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def broadcast_runs(path: Path, station_id: str) -> list[tuple[float, float]]:
    """같은 방송 ID의 방송 회차 — [시작, 끝) 구간. 백엔드는 고정 채널 ID를 다시 쓴다.

    회차는 station.created(없으면 station.started)에서 시작한다. 시작 전 입력과 사연도 그 회차에 들어간다.
    station_log가 없는 예전 DB는 전체가 한 회차다.
    """
    starts: list[float] = []
    open_created = False  # created 뒤 아직 started가 없다 — 그 started는 같은 회차다
    for at, event in _read(path, "SELECT at, event FROM station_log WHERE station_id = ? "
                                 "AND event IN ('station.created', 'station.started') ORDER BY rowid",
                           (station_id,)):
        if event == "station.created" or not open_created:
            starts.append(at)
        open_created = event == "station.created"
    if not starts:
        return [(float("-inf"), float("inf"))]
    starts[0] = float("-inf")  # 첫 회차 전 기록(예: 이 기능 전의 방송)은 첫 회차에 넣는다
    return list(zip(starts, [*starts[1:], float("inf")]))


def list_stations(path: Path) -> list[dict]:
    """기록이 있는 방송 ID와 회차 수 — 생성 기록 기준. 예전 DB에는 station_log가 없을 수 있다."""
    rows = _read(path, "SELECT station_id, COUNT(DISTINCT job_id) FROM generation_log "
                       "GROUP BY station_id ORDER BY MIN(started_at)", ())
    return [{"station_id": sid, "jobs": n, "runs": len(broadcast_runs(path, sid))} for sid, n in rows]


def station_report(sqlite_path: str | Path, station_id: str, run: int = -1) -> dict:
    """run: 회차 번호(0부터). 기본은 마지막 회차."""
    path = Path(sqlite_path)
    if not path.exists():
        raise FileNotFoundError(path)
    runs = broadcast_runs(path, station_id)
    start, end = runs[run]

    def q(sql: str, at: str = "at") -> list[tuple]:
        """{w}에 회차 조건이 들어간다. 테이블이 없으면(그 기능 전에 만든 DB) 빈 결과다."""
        window = f"AND {at} >= ? AND {at} < ?"
        return _read(path, sql.format(w=window), (station_id, start, end))

    def one(sql: str, empty: tuple, at: str = "at") -> tuple:
        rows = q(sql, at)
        return rows[0] if rows else empty

    started = one("SELECT MIN(at) FROM station_log WHERE station_id = ? {w} AND event = 'station.started'",
                  (None,))[0]
    aired = one("SELECT MIN(at), MAX(at), COUNT(*) FROM aired_log WHERE station_id = ? {w}", (None, None, 0))
    topic = q("SELECT payload_json FROM station_log WHERE station_id = ? {w} AND event = 'station.started'")

    inputs: dict[str, dict[str, int]] = {}
    for kind, route, n in q("SELECT input_kind, route, COUNT(*) FROM input_log WHERE station_id = ? {w} "
                            "GROUP BY input_kind, route", "received_at"):
        inputs.setdefault(kind or "unknown", {})[route or "pool"] = n
    # 요청마다 마지막 상태 — rowid가 기록 순서다
    final_states: dict[str, int] = {}
    for state, n in q("SELECT state, COUNT(*) FROM request_log r WHERE station_id = ? {w} AND rowid = "
                      "(SELECT MAX(rowid) FROM request_log WHERE request_id = r.request_id "
                      "AND station_id = r.station_id) GROUP BY state"):
        final_states[state] = n

    arrived = dict(q("SELECT chat_type, COUNT(*) FROM input_log WHERE station_id = ? {w} GROUP BY chat_type",
                     "received_at"))
    reflected = q("SELECT chat_type, quote_age, quote_age_net, topic_match FROM reflection_log "
                  "WHERE station_id = ? {w}", "started_at")
    reactions = [(net, match) for c, _, net, match in reflected if c == "reaction"]

    def rate(chat_type: str) -> float | None:
        return _ratio(sum(1 for c, *_ in reflected if c == chat_type), arrived.get(chat_type, 0))

    reflection = {
        "reaction_reflection_rate": rate("reaction"),
        "standalone_reflection_rate": rate("standalone"),
        "message_reflection_rate": rate("message"),
        "timely_rate": _ratio(sum(1 for net, _ in reactions if net <= Q_MAX_SEC), len(reactions)),
        "topic_match_rate": _ratio(sum(1 for _, match in reactions if match), len(reactions)),
        "age_sec": {t: _spread([a for c, a, *_ in reflected if c == t])
                    for t in sorted({c for c, *_ in reflected})},
        "age_net_sec": _spread([net for _, _, net, _ in reflected if net is not None]),
    }

    corners: dict[str, dict] = {}
    open_runs: dict[str, int] = {}
    durations: dict[str, list[float]] = {}  # exit − enter — length가 없는 예전 DB용. 앞 곡이 섞인다
    lengths: dict[str, list[float]] = {}  # 코너가 낸 멘트 길이 (#220)
    for corner, event, produced_ms in q("SELECT corner_type, event, produced_ms FROM corner_log "
                                        "WHERE station_id = ? {w} ORDER BY rowid"):
        if event == "length":
            lengths.setdefault(corner, []).append(produced_ms / 1000)
            continue
        counts = corners.setdefault(corner, {"enter": 0, "exit": 0, "skip": 0, "fallback": 0})
        counts[event] = counts.get(event, 0) + 1
        if event == "enter":
            open_runs[corner] = produced_ms
        elif event == "exit" and corner in open_runs:
            durations.setdefault(corner, []).append((produced_ms - open_runs.pop(corner)) / 1000)
    for corner, counts in corners.items():
        counts["produced_sec"] = _spread(lengths.get(corner) or durations.get(corner, []))
    turns = sum(c["enter"] + c["skip"] for c in corners.values())
    corner_rates = {
        # 대체는 원래 코너의 차례가 대체 코너 진입으로 이어진 것이다 — 진입 수에 이미 들어 있다
        "turns": turns,
        "skip_rate": _ratio(sum(c["skip"] for c in corners.values()), turns),
        "fallback_rate": _ratio(sum(c["fallback"] for c in corners.values()), turns),
    }

    jobs: dict[str, dict] = {}
    stages: dict[str, list[float]] = {}
    outcomes = {"ok": 0, "fallback": 0, "reject": 0, "incomplete": 0, "error": 0}
    quotes = {"ok": 0, "missing": 0}  # 넣으라고 준 청취자 글이 대본에 들어갔는가 (파이프라인 인용 검사)
    for job_id, stage, started_at, finished_at, result in q(
            "SELECT job_id, stage, started_at, finished_at, result FROM generation_log "
            "WHERE station_id = ? {w} ORDER BY rowid", "started_at"):
        elapsed = finished_at - started_at
        job = jobs.setdefault(job_id, {"total": 0.0, "outcome": "incomplete"})
        if stage == "quote" and result in quotes:
            quotes[result] += 1
        if stage in ("llm", "l2", "tts") and result == "ok":
            job["total"] += elapsed
            stages.setdefault(stage, []).append(elapsed)
        if stage == "fallback":
            job["outcome"] = "fallback"
        elif result == "reject":
            job["outcome"] = "reject"
        elif stage == "tts" and result == "ok" and job["outcome"] == "incomplete":
            job["outcome"] = "ok"
        if result == "error":
            outcomes["error"] += 1
    for job in jobs.values():
        outcomes[job["outcome"]] += 1  # incomplete: 방송이 끝날 때 생성 중이던 것
    generation = {
        "jobs": len(jobs),
        "outcomes": outcomes,
        "fallback_rate": _ratio(outcomes["fallback"], len(jobs)),
        "stage_sec": {s: _spread(v) for s, v in sorted(stages.items())},
        "job_sec": _spread([j["total"] for j in jobs.values() if j["outcome"] == "ok"]),
        # 인용 누락 — 빠진 입력은 한 번 되돌렸다가 또 빠지면 거절된다. 실제 LLM에서만 생긴다
        "quote_checks": sum(quotes.values()),
        "quote_missing_rate": _ratio(quotes["missing"], sum(quotes.values())),
        # 빠진 인용의 채팅과 대본 — 건너뛴 것인지, 풀어 말해서 검사가 못 알아본 것인지 읽어서 가른다 (#258)
        "quote_missing": [
            {"corner_type": corner, "step": step, "kind": kind, "match": match, "chat": chat, "script": script}
            for corner, step, kind, match, chat, script in q(
                "SELECT corner_type, step, kind, match_ratio, chat, script FROM quote_log "
                "WHERE station_id = ? {w} AND result = 'missing' ORDER BY rowid")],
        # 이야기 단계가 무엇을 소재로 말했는지 — chat·callback·angle·topic (CORNER_DESIGN 7.7절, #226)
        "talk_material": dict(q(
            "SELECT json_extract(decision_json, '$.material') AS m, COUNT(*) FROM decision_log "
            "WHERE station_id = ? {w} AND json_extract(decision_json, '$.action') = 'generate' "
            "AND m IS NOT NULL GROUP BY m")),
    }

    stories = {f"{method}_{'relevant' if rel else 'dropped'}": n for method, rel, n in q(
        "SELECT method, relevant, COUNT(*) FROM story_judge_log WHERE station_id = ? {w} "
        "GROUP BY method, relevant")}
    steer = one("SELECT COUNT(*) FROM steer_log WHERE station_id = ? {w}", (0,))[0]
    aggregate = dict(q("SELECT kind, COUNT(*) FROM aggregate_log WHERE station_id = ? {w} GROUP BY kind"))
    topic_name = json.loads(topic[0][0]).get("topic") if topic else None
    # 고정 문구가 나간 횟수 — resume("다시 …로 돌아왔어요")이 곡마다 나오면 토막 난 방송이다 (#262)
    glue = {str(step).removeprefix("glue:"): n for step, n in q(
        "SELECT json_extract(decision_json, '$.step') AS s, COUNT(*) FROM decision_log "
        "WHERE station_id = ? {w} AND json_extract(decision_json, '$.action') = 'glue' GROUP BY s")}
    # 접착 문구(고정 문구)는 대본 기록이 없다 — 일부러 같은 말을 하는 것이라 반복 지표에서 빠지는 게 맞다
    scripts = [s for (s,) in q("SELECT script FROM aired_log WHERE station_id = ? {w} ORDER BY at")]

    return {
        "station_id": station_id,
        "run": {"index": run % len(runs), "count": len(runs)},
        "topic": topic_name,
        "started_at": started,
        "aired": {"segments": aired[2], "first_at": aired[0], "last_at": aired[1],
                  "span_sec": round(aired[1] - aired[0], 1) if aired[2] else None},
        "inputs": inputs,
        "request_final_states": final_states,
        "reflection": reflection,
        "corners": corners,
        "corner_rates": corner_rates,
        "generation": generation,
        "stories": stories,
        "steer": steer,
        "aggregate": aggregate,
        "glue": {"resume": glue.get("resume", 0), **glue},
        "scripts": script_stats(scripts, topic_name),
    }


def _fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _pct(value) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def render(report: dict) -> str:
    """사람이 읽는 요약 — 마크다운 표. 원자료는 --json으로 본다."""
    r = report
    lines = [f"# 방송 리포트 · {r['station_id']}", "",
             f"- 회차: {r['run']['index'] + 1} / {r['run']['count']}", f"- 주제: {r['topic'] or '-'}",
             (f"- 송출 세그먼트: {r['aired']['segments']}개, 첫 송출부터 마지막 송출까지 "
              f"{_fmt(r['aired']['span_sec'])}초"), ""]

    lines += ["## 입력", "", "| 종류 | 경로별 건수 |", "|---|---|"]
    lines += [f"| {kind} | {', '.join(f'{route} {n}' for route, n in routes.items())} |"
              for kind, routes in sorted(r["inputs"].items())] or ["| - | - |"]
    states = ", ".join(f"{s} {n}" for s, n in sorted(r["request_final_states"].items())) or "-"
    lines += ["", f"요청 최종 상태: {states}", ""]

    ref = r["reflection"]
    lines += ["## 반영", "", "| 지표 | 값 |", "|---|---|",
              f"| 반응형 채팅 반영률 | {_pct(ref.get('reaction_reflection_rate'))} |",
              f"| 독립형 채팅 반영률 | {_pct(ref.get('standalone_reflection_rate'))} |",
              f"| 문자 반영률 | {_pct(ref.get('message_reflection_rate'))} |",
              f"| 제때 반영 (반응형, {Q_MAX_SEC:.0f}초 이내) | {_pct(ref.get('timely_rate'))} |",
              f"| 화제 일치 (반응형) | {_pct(ref.get('topic_match_rate'))} |",
              (f"| 반영까지 걸린 시간 (방송 지연 제외) 중앙값 / P95 | "
               f"{_fmt(ref['age_net_sec']['p50'])} / {_fmt(ref['age_net_sec']['p95'])}초 |"), ""]

    rates = r["corner_rates"]
    lines += ["## 코너", "", (f"차례 {rates['turns']}번 · 건너뜀 {_pct(rates['skip_rate'])} · "
                              f"대체 {_pct(rates['fallback_rate'])}"), "",
              "| 코너 | 진입 | 건너뜀 | 대체 | 길이 중앙값 (초) |", "|---|---|---|---|---|"]
    lines += [f"| {corner} | {c['enter']} | {c['skip']} | {c['fallback']} | {_fmt(c['produced_sec']['p50'])} |"
              for corner, c in sorted(r["corners"].items())]

    gen = r["generation"]
    out = gen["outcomes"]
    lines += ["", "## 생성", "",
              (f"세그먼트 {gen['jobs']}개 · 정상 {out['ok']} · 대체 멘트 {out['fallback']} "
               f"({_pct(gen['fallback_rate'])}) · 거부 {out['reject']} · 미완료 {out['incomplete']} · "
               f"단계 오류(재시도 포함) {out['error']}"),
              "",
              "| 단계 | 건수 | 중앙값 (초) | P95 (초) |", "|---|---|---|---|"]
    rows = [*gen["stage_sec"].items(), ("세그먼트 합계", gen["job_sec"])]
    lines += [f"| {name} | {s['count']} | {_fmt(s['p50'])} | {_fmt(s['p95'])} |" for name, s in rows]
    material = ", ".join(f"{_MATERIALS.get(k, k)} {n}" for k, n in sorted(gen["talk_material"].items())) or "-"
    lines += ["", f"이야기 단계 소재: {material}",
              f"인용 검사: {gen.get('quote_checks', 0)}건 · 누락 {_pct(gen.get('quote_missing_rate'))}"]
    for miss in gen.get("quote_missing") or []:
        lines += [f"- 누락 · {miss['corner_type']} · 일치 {miss['match']:.2f} · \"{miss['chat']}\"",
                  f"  > {miss['script']}"]

    sc = r["scripts"]
    lines += ["", "## 멘트", "", "| 지표 | 값 |", "|---|---|",
              f"| 송출된 멘트 (고정 문구 제외) | {sc['segments']} |",
              f"| 같은 첫머리로 시작한 멘트 | {_pct(sc['same_opening_rate'])} |",
              f"| 직전 멘트와의 유사도 평균 (0~1) | {_fmt(sc['similarity_mean'])} |",
              f"| 주제 소개 횟수 (1이 정상) | {_fmt(sc['topic_intros'])} |",
              f"| 질문으로 끝난 멘트 | {_pct(sc.get('question_end_rate'))} |",
              f"| 고정 문구 \"다시 …로 돌아왔어요\" | {(r.get('glue') or {}).get('resume', '-')}번 |"]

    stories = ", ".join(f"{k} {n}" for k, n in sorted(r["stories"].items())) or "-"
    aggregate = ", ".join(f"{k} {n}" for k, n in sorted(r["aggregate"].items())) or "-"
    lines += ["", "## 그 밖의 반영", "", f"- 사연 판정: {stories}", f"- 화제 전환: {r['steer']}회",
              f"- 집계 언급·예고: {aggregate}"]
    return "\n".join(lines) + "\n"


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onair-report", description="방송 1회의 정량 지표")
    parser.add_argument("station_id", nargs="?", help="방송 ID. 없으면 방송 목록을 보여 준다")
    parser.add_argument("--db", default="var/engine_metrics.sqlite", help="계측 SQLite 경로")
    parser.add_argument("--run", type=int, default=0,
                        help="같은 방송 ID의 회차 (1부터, 음수는 뒤에서부터). 기본은 마지막 회차")
    parser.add_argument("--json", action="store_true", help="원자료를 JSON으로")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    path = Path(args.db)
    if not path.exists():
        print(f"계측 DB가 없습니다: {path}", file=sys.stderr)
        return 1
    if args.station_id is None:
        for s in list_stations(path):
            print(f"{s['station_id']}\t회차 {s['runs']}번 · 세그먼트 생성 {s['jobs']}건")
        return 0
    runs = len(broadcast_runs(path, args.station_id))
    run = -1 if args.run == 0 else args.run - 1 if args.run > 0 else args.run
    if not -runs <= run < runs:
        print(f"회차는 1~{runs}입니다", file=sys.stderr)
        return 1
    report = station_report(path, args.station_id, run)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
