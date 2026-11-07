"""시나리오 실행 — 회차마다 엔진 하나를 실시간으로 돌리고 결과를 파일로 남긴다.

출력 (out/<실행 시각>/<시나리오>/):
  run-N/transcript.md  송출 시각 순 대본 — 코너, 들어온 입력, 인용된 입력, 무음, 곡
  run-N/report.md      onair-report 지표 + 무음 지표 + 기대 결과 판정 (report.json은 원자료)
  run-N/run.json       재현 정보 — 시나리오 원문, seed, git 커밋, LLM, TTS
  run-N/events.jsonl   엔진 패널 이벤트 (엔진에 event_log가 있을 때)
  run-N/broadcast.m3u  실제 TTS 회차의 멘트 오디오 재생 목록 (곡은 없다)
  summary.md           회차별 핵심 지표와 기대 결과 통과 수
  issues.csv           빈 문제 기록표
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import telemetry as telemetry_module
from ..domain import StationConfig
from ..engine import EngineSettings, StationEngine
from ..main import add_llm_args, apply_llm_args, load_env, load_profile
from ..report import render, station_report
from ..telemetry import _read
from . import review
from .sim import Played, SimBackend, SimTts
from .spec import ScenarioSpec, TrackSpec, load_scenario

ENGINE_ROOT = Path(__file__).resolve().parents[3]  # apps/engine
CALLS_PER_MINUTE = 2.5  # 실제 LLM 확인 프롬프트의 추정치 — 2026-11-07 실측(호출 13회)과 말:곡 5:5 기준


def _clock(sec: float) -> str:
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=ENGINE_ROOT, timeout=5, check=False).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _settings(args: argparse.Namespace, run_dir: Path) -> EngineSettings:
    settings = EngineSettings(
        audio_dir=run_dir / "audio", sqlite_path=run_dir / "metrics.sqlite",
        safety_rules_path=ENGINE_ROOT / "config" / "safety_rules.yaml",
        formats_dir=ENGINE_ROOT / "config" / "formats",
        chat_rules_path=ENGINE_ROOT / "config" / "chat_rules.yaml",
        news_feeds_path=ENGINE_ROOT / "config" / "news_feeds.yaml",
        llm="openai", llm_model="gpt-6-luna", llm_reasoning_effort="low",  # 팀 결정 LLM (확인 3)
        tts="gemini" if args.tts == "gemini" else "dummy",
        # 실제 TTS는 리허설끼리 캐시를 나눠 한도를 아낀다. 흉내 TTS는 캐시하지 않는다
        tts_cache_dir=ENGINE_ROOT / "var" / "tts_cache" if args.tts == "gemini" else None,
        dummy_tracks=False,  # 곡은 가짜 백엔드가 track.queued로 알린다
    )
    apply_llm_args(settings, args)
    return settings


async def _inject(spec: ScenarioSpec, backend: SimBackend, engine: StationEngine,
                  rng: random.Random, finished: asyncio.Event) -> None:
    """시각별 입력과 배경 채팅을 station.started 기준으로 보낸다."""
    await backend.started.wait()
    for i, story in enumerate(spec.board_stories):  # 백엔드는 station.started 직후 후보를 보낸다 (#196)
        engine.on_board_story({"story_id": f"story_sim_{i}", "body": story.body,
                               "nickname": story.nickname, "posted_at": time.time() - 3600})
    events = [(item.at, item) for item in spec.inputs]
    if spec.chatter is not None:
        c = spec.chatter
        end = c.end if c.end is not None else spec.station.broadcast_minutes * 60
        at = c.start + rng.expovariate(c.per_minute / 60)
        n = 0
        while at < end:
            events.append((at, ("chatter", n, rng.choice(c.lines), f"crowd{rng.randrange(c.people)}")))
            at += rng.expovariate(c.per_minute / 60)
            n += 1
    events.sort(key=lambda e: e[0])
    for at, item in events:
        wait = backend.started_at + at - time.time()
        if wait > 0:
            await SimBackend._wait_any(finished, timeout=wait)
        if finished.is_set():
            return
        if isinstance(item, tuple):  # 배경 채팅
            _, n, body, who = item
            rid = f"chatter_{n}"
            backend.played.append(Played(backend.now(), "input", 0, text=f"채팅 {rid} ({who}): {body}"))
            await engine.submit_request("chat", body, who, request_id=rid)
            continue
        rid = f"in_{item.id}"
        shown = item.body if item.kind != "song_request" else f"{item.title} - {item.artist} / {item.body}"
        name = f"{item.who}, {item.nickname}" if item.nickname else item.who
        backend.played.append(Played(backend.now(), "input", 0, text=f"{_KINDS[item.kind]} {rid} ({name}): {shown}"))
        await engine.submit_request(item.kind, item.body, item.who, request_id=rid, nickname=item.nickname)
        if item.kind == "song_request":  # 백엔드는 신청곡을 다음 곡으로 올리고 track.queued로 알린다
            backend.queue_next_track({"track": TrackSpec(title=item.title, artist=item.artist, sec=item.sec),
                                      "request_id": rid, "nickname": item.nickname})


_KINDS = {"chat": "채팅", "message": "문자", "song_request": "신청곡"}


async def run_one(spec: ScenarioSpec, index: int, run_dir: Path, args: argparse.Namespace) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    seed = spec.sim.seed + index
    rng = random.Random(seed)
    station_id = f"sim_{spec.name}_{index + 1}"
    st = spec.station
    profile_path = Path(args.persona or st.persona)
    profile_path = profile_path if profile_path.is_absolute() else ENGINE_ROOT / profile_path
    config = StationConfig(station_id=station_id, profile=load_profile(profile_path),
                           broadcast_minutes=args.minutes or st.broadcast_minutes, format=st.format,
                           corners=st.corners, intensity=st.intensity, topic=st.topic,
                           voice=st.voice if args.tts == "gemini" else None)
    settings = _settings(args, run_dir)
    backend = SimBackend(sim=spec.sim, tracks=spec.tracks, rng=random.Random(seed * 7919))
    engine = StationEngine(config, settings, backend)
    backend.engine = engine
    sim_tts = None
    if args.tts == "sim":
        sim_tts = SimTts(spec.sim, random.Random(seed * 104729))
        engine.scheduler.pipeline.tts._inner = sim_tts  # 파이프라인과 접착 문구가 같은 CachedTts를 쓴다
    backend.queue_next_track()  # 백엔드는 방송 전에 첫 소개 곡을 정해 둔다

    finished = backend.finished
    started = time.time()

    async def broadcast():
        try:
            await engine.run()
        finally:
            finished.set()  # 엔진 계측이 닫혔다 — 가짜 백엔드는 더 알리지 않는다

    tasks = [asyncio.create_task(broadcast()), asyncio.create_task(backend.play(finished)),
             asyncio.create_task(backend.report_backpressure(finished)),
             asyncio.create_task(_inject(spec, backend, engine, rng, finished))]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    error = next((repr(r) for r in results if isinstance(r, BaseException)), None)

    db = settings.sqlite_path
    report = station_report(db, station_id) if db.exists() else {}
    silences = [p.sec for p in backend.played if p.kind == "silence"]
    report["sim"] = {
        "silence_total_sec": round(sum(silences), 1), "silence_max_sec": round(max(silences, default=0.0), 1),
        "silence_count": len(silences),
        "speech_segments": sum(1 for p in backend.played if p.kind == "speech"),
        "songs": sum(1 for p in backend.played if p.kind == "music"),
        "wall_sec": round(time.time() - started, 1),
        "tts_latency_sec": _mean(sim_tts.latencies) if sim_tts else None,
        "error": error,
    }
    reflected = _reflections(db, station_id)
    report["expect"] = _check(spec, backend, report, reflected)
    _write_run(run_dir, spec, index, seed, args, report, backend, reflected, db, station_id)
    if args.tts == "sim":
        shutil.rmtree(settings.audio_dir, ignore_errors=True)  # 무음 파일 — 남길 이유가 없다
    return report


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _reflections(db: Path, station_id: str) -> dict[str, tuple[str, float]]:
    """request_id → (세그먼트, 도착부터 재생 시작까지 초)."""
    rows = _read(db, "SELECT request_id, segment_id, quote_age FROM reflection_log WHERE station_id = ?",
                 (station_id,)) if db.exists() else []
    return {rid: (seg, age) for rid, seg, age in rows}


def _check(spec: ScenarioSpec, backend: SimBackend, report: dict,
           reflected: dict[str, tuple[str, float]]) -> list[dict]:
    aired = " ".join(p.script or "" for p in backend.played if p.kind == "speech")
    out = []
    for e in spec.expect:
        if e.type == "reflected":
            hit = reflected.get(f"in_{e.input}")
            passed = hit is not None and (e.within_sec is None or hit[1] <= e.within_sec)
            detail = "반영 안 됨" if hit is None else f"{hit[1]:.0f}초 뒤 재생"
        elif e.type == "aired_contains":
            passed, detail = e.text in aired, ""
        elif e.type == "not_aired":
            passed, detail = e.text not in aired, ""
        elif e.type == "max_silence_sec":
            value = report["sim"]["silence_max_sec"]
            passed, detail = value <= e.value, f"최대 무음 {value}초"
        else:  # max_fallback
            value = report.get("generation", {}).get("outcomes", {}).get("fallback", 0)
            passed, detail = value <= e.value, f"대체 멘트 {value}건"
        out.append({"type": e.type, "target": e.input or e.text or e.value, "note": e.note,
                    "passed": passed, "detail": detail})
    return out


def _transcript(spec: ScenarioSpec, backend: SimBackend, reflected: dict[str, tuple[str, float]],
                titles: dict[str, str]) -> str:
    by_segment: dict[str, list[str]] = {}
    for rid, (seg, _) in reflected.items():
        by_segment.setdefault(seg, []).append(rid)
    lines = [f"# 대본 · {spec.name}", "", "시각은 방송 시작(station.started) 기준이다. 청취자는 방송 지연만큼 늦게 듣는다.", ""]
    for p in sorted(backend.played, key=lambda p: p.at):
        t = _clock(p.at)
        if p.kind == "input":
            lines.append(f"- `{t}` ▶ 입력 · {p.text}")
        elif p.kind == "silence":
            lines.append(f"- `{t}` ⏸ **무음 {p.sec:.1f}초**")
        elif p.kind == "music":
            lines.append(f"- `{t}` ♪ 곡 · {p.text} ({_clock(p.sec)})")
        else:
            corner = titles.get(p.corner_type or "", p.corner_type)
            quoted = f" · 반영: {', '.join(by_segment[p.segment_id])}" if p.segment_id in by_segment else ""
            body = p.script if p.script is not None else "(고정 문구 또는 엔진 종료 뒤 송출)"
            lines.append(f"- `{t}` **[{corner}]** ({p.sec:.0f}초){quoted}  \n  {body}")
    return "\n".join(lines) + "\n"


def _played(backend: SimBackend, reflected: dict[str, tuple[str, float]], titles: dict[str, str]) -> list[dict]:
    by_segment: dict[str, list[str]] = {}
    for rid, (seg, _) in reflected.items():
        by_segment.setdefault(seg, []).append(rid)
    return [{"at": round(p.at, 2), "kind": p.kind, "sec": round(p.sec, 2), "segment_id": p.segment_id,
             "corner_type": p.corner_type, "corner": titles.get(p.corner_type or "", p.corner_type),
             "script": p.script, "text": p.text, "reflected": by_segment.get(p.segment_id or "", [])}
            for p in sorted(backend.played, key=lambda p: p.at)]


def _write_run(run_dir: Path, spec: ScenarioSpec, index: int, seed: int, args: argparse.Namespace,
               report: dict, backend: SimBackend, reflected: dict, db: Path, station_id: str) -> None:
    titles = backend.engine.telemetry.corner_titles if hasattr(backend.engine.telemetry, "corner_titles") else {}
    titles = titles or {name: c.title for name, c in backend.engine.scheduler.fmt.corners.items()}
    (run_dir / "transcript.md").write_text(_transcript(spec, backend, reflected, titles), encoding="utf-8")
    (run_dir / "played.json").write_text(json.dumps(  # 대본 검토 페이지(review.py)의 원자료
        {"played": _played(backend, reflected, titles)}, ensure_ascii=False), encoding="utf-8")
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    text = render(report) if "generation" in report else "# 리포트 없음\n"
    sim = report["sim"]
    text += (f"\n## 시뮬레이션\n\n- 무음: 합계 {sim['silence_total_sec']}초, 최대 {sim['silence_max_sec']}초, "
             f"{sim['silence_count']}번\n- 송출: 멘트 {sim['speech_segments']}개, 곡 {sim['songs']}곡\n"
             f"- 실행 시간: {sim['wall_sec']}초\n- TTS 흉내 평균 지연: {sim['tts_latency_sec'] or '-'}초\n")
    if sim["error"]:
        text += f"- **오류**: {sim['error']}\n"
    if report["expect"]:
        text += "\n## 기대 결과\n\n| 판정 | 종류 | 대상 | 설명 | 결과 |\n|---|---|---|---|---|\n"
        text += "".join(f"| {'통과' if e['passed'] else '**실패**'} | {e['type']} | {e['target']} | "
                        f"{e['note'] or ''} | {e['detail']} |\n" for e in report["expect"])
    (run_dir / "report.md").write_text(text, encoding="utf-8")
    (run_dir / "run.json").write_text(json.dumps({
        "scenario": spec.model_dump(), "run": index + 1, "seed": seed, "git": _git_commit(),
        "llm": {"kind": args.llm or "openai", "model": args.llm_model or "gpt-6-luna"},
        "tts": args.tts, "persona": args.persona or spec.station.persona,
        "minutes": args.minutes or spec.station.broadcast_minutes,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    read_events = getattr(telemetry_module, "read_events", None)  # 엔진 패널 이벤트(#208)가 있을 때만
    if read_events is not None and db.exists():
        with open(run_dir / "events.jsonl", "w", encoding="utf-8") as f:
            f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in read_events(db, station_id, limit=100000))
    if args.tts == "gemini":
        audio_root = backend.engine.settings.audio_dir
        refs = [p.audio_ref for p in sorted(backend.played, key=lambda p: p.at) if p.audio_ref]
        (run_dir / "broadcast.m3u").write_text(
            "\n".join(str((audio_root / r).resolve()) for r in refs) + "\n", encoding="utf-8")


_SUMMARY_COLUMNS = [
    ("생성", lambda r: r.get("generation", {}).get("jobs")),
    ("대체 멘트", lambda r: r.get("generation", {}).get("outcomes", {}).get("fallback")),
    ("무음 합계(초)", lambda r: r["sim"]["silence_total_sec"]),
    ("최대 무음(초)", lambda r: r["sim"]["silence_max_sec"]),
    ("반응형 반영률", lambda r: r.get("reflection", {}).get("reaction_reflection_rate")),
    ("독립형 반영률", lambda r: r.get("reflection", {}).get("standalone_reflection_rate")),
    ("문자 반영률", lambda r: r.get("reflection", {}).get("message_reflection_rate")),
    ("반영 시간 중앙값(초)", lambda r: r.get("reflection", {}).get("age_net_sec", {}).get("p50")),
    ("코너 대체율", lambda r: r.get("corner_rates", {}).get("fallback_rate")),
    ("세그먼트 생성 P95(초)", lambda r: r.get("generation", {}).get("job_sec", {}).get("p95")),
    ("화제 전환", lambda r: r.get("steer")),
    ("기대 결과", lambda r: f"{sum(e['passed'] for e in r['expect'])}/{len(r['expect'])}" if r["expect"] else "-"),
]


def write_summary(out: Path, spec: ScenarioSpec, reports: list[dict]) -> None:
    header = "| 회차 | " + " | ".join(name for name, _ in _SUMMARY_COLUMNS) + " |"
    lines = [f"# 요약 · {spec.name}", "", spec.description, "", header,
             "|" + "---|" * (len(_SUMMARY_COLUMNS) + 1)]
    for i, r in enumerate(reports, 1):
        cells = [_cell(get(r)) for _, get in _SUMMARY_COLUMNS]
        lines.append(f"| {i}{' ⚠' if r['sim']['error'] else ''} | " + " | ".join(cells) + " |")
    numeric = []
    for name, get in _SUMMARY_COLUMNS:
        values = [v for v in (get(r) for r in reports) if isinstance(v, (int, float))]
        numeric.append(f"{_cell(min(values))} / {_cell(sum(values) / len(values))} / {_cell(max(values))}"
                       if values else "-")
    lines.append("| 최소 / 평균 / 최대 | " + " | ".join(numeric) + " |")
    if spec.expect:
        lines += ["", "## 기대 결과 통과 회차", "", "| 기대 결과 | 통과 |", "|---|---|"]
        for j, e in enumerate(spec.expect):
            passed = sum(1 for r in reports if r["expect"][j]["passed"])
            lines.append(f"| {e.note or e.type} ({e.input or e.text or e.value}) | {passed}/{len(reports)} |")
    errors = [f"- 회차 {i}: {r['sim']['error']}" for i, r in enumerate(reports, 1) if r["sim"]["error"]]
    if errors:
        lines += ["", "## 오류", "", *errors]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with open(out / "issues.csv", "w", encoding="utf-8-sig", newline="") as f:  # 엑셀이 한글을 읽게 BOM
        csv.writer(f).writerow(["시나리오", "회차", "시각", "세그먼트", "증상 (대본 일부)", "분류", "재현 횟수", "메모"])


def _cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


async def run_all(specs: list[ScenarioSpec], args: argparse.Namespace, out: Path) -> dict[str, list[dict]]:
    slots = asyncio.Semaphore(args.parallel)
    results: dict[str, list[dict | None]] = {s.name: [None] * args.repeat for s in specs}

    async def one(spec: ScenarioSpec, i: int) -> None:
        async with slots:
            print(f"시작 · {spec.name} {i + 1}/{args.repeat}", flush=True)
            results[spec.name][i] = await run_one(spec, i, out / spec.name / f"run-{i + 1}", args)
            sim = results[spec.name][i]["sim"]
            print(f"끝 · {spec.name} {i + 1}/{args.repeat} · 무음 {sim['silence_total_sec']}초"
                  + (f" · 오류 {sim['error']}" if sim["error"] else ""), flush=True)

    await asyncio.gather(*(one(s, i) for s in specs for i in range(args.repeat)))
    for spec in specs:
        write_summary(out / spec.name, spec, results[spec.name])
    return results


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onair-scenario", description="방송 시나리오 반복 실행 (실시간)")
    parser.add_argument("scenarios", nargs="+", type=Path, help="시나리오 파일 (config/scenarios/*.yaml)")
    parser.add_argument("--repeat", type=int, default=5, help="시나리오마다 회차 수")
    parser.add_argument("--parallel", type=int, default=10, help="동시에 돌릴 회차 수")
    parser.add_argument("--minutes", type=int, default=None, help="방송 길이를 덮어쓴다 (점검용)")
    parser.add_argument("--persona", default=None, help="persona 파일을 덮어쓴다 (페르소나 비교용)")
    parser.add_argument("--tts", choices=["sim", "gemini"], default="sim",
                        help="sim: 흉내(무료, 기본) / gemini: 실제 TTS (한도 사용)")
    parser.add_argument("--out", type=Path, default=ENGINE_ROOT / "var" / "scenario")
    parser.add_argument("--yes", action="store_true", help="실제 LLM·TTS 확인을 건너뛴다")
    add_llm_args(parser)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    specs = [load_scenario(p) for p in args.scenarios]
    runs = len(specs) * args.repeat
    minutes = max(args.minutes or s.station.broadcast_minutes for s in specs)
    if (args.llm or "openai") != "dummy" or args.tts == "gemini":
        calls = sum((args.minutes or s.station.broadcast_minutes) * CALLS_PER_MINUTE for s in specs) * args.repeat
        print(f"{runs}회 실행 · 동시 {args.parallel}회 · 예상 시간 약 {-(-runs // args.parallel) * (minutes + 2)}분 · "
              f"예상 LLM 호출 약 {calls:.0f}회" + (" · 실제 TTS 사용" if args.tts == "gemini" else ""))
        if not args.yes:
            if not sys.stdin.isatty():
                print("실제 LLM·TTS를 쓰려면 --yes가 필요합니다", file=sys.stderr)
                return 1
            if input("진행할까요? [y/N] ").strip().lower() != "y":
                return 1
    out = args.out / time.strftime("%Y%m%d-%H%M%S")
    asyncio.run(run_all(specs, args, out))
    review.write([out], out / "review.html")
    print(f"결과: {out}\n대본 검토: {out / 'review.html'}")
    return 0
