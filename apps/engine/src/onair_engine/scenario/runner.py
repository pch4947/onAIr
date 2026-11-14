"""시나리오 실행 — 회차마다 엔진 하나를 실시간으로 돌리고 결과를 파일로 남긴다.

출력 (out/<실행 시각>/<시나리오>/ — 변형이 여럿이면 out/<실행 시각>/<변형>/<시나리오>/):
  run-N/transcript.md  송출 시각 순 대본 — 코너, 들어온 입력, 인용된 입력, 무음, 곡
  run-N/report.md      onair-report 지표 + 무음 지표 + 기대 결과 판정 (report.json은 원자료)
  run-N/run.json       재현 정보 — 시나리오 원문, seed, git 커밋, LLM, TTS
  run-N/events.jsonl   엔진 패널 이벤트 (엔진에 event_log가 있을 때)
  run-N/broadcast.m3u  실제 TTS 회차의 멘트 오디오 재생 목록 (곡은 없다)
  summary.md           회차별 핵심 지표와 기대 결과 통과 수
  issues.csv           빈 문제 기록표
실행마다 (out/<실행 시각>/):
  session.json         무엇을 어떻게 돌렸나 — 명령, 커밋, 변형, 목적(--note)
  worktree.diff        커밋하지 않은 엔진 변경 (있을 때만) — 프롬프트를 고쳐 보는 중의 실행도 재현된다
  compare.md·json      변형이 여럿일 때 base 대비 지표 비교 (compare.py)
  review.html          대본 검토 페이지 (review.py)
그리고 out/ledger.jsonl에 변형 × 시나리오마다 한 줄씩 쌓는다 — 지금까지의 실행 전체를 훑는 장부다.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path

import yaml

from .. import provenance
from .. import telemetry as telemetry_module
from ..domain import StationConfig
from ..engine import EngineSettings, StationEngine
from ..inputs.rules import ChatRules
from ..main import add_llm_args, apply_llm_args, load_env, load_profile
from ..pipeline.tts import DEFAULT_GEMINI_VOICE
from ..report import render, station_report
from ..sources.rss import load_news_feeds
from ..telemetry import _read
from . import compare, review
from .metrics import METRICS, aggregate
from .sim import LoopLag, Played, SimBackend, SimTts, tune_dummy_llm
from .spec import (
    ControlSpec,
    ScenarioSpec,
    SimSpec,
    StationSpec,
    StorySpec,
    TrackSpec,
    load_scenario,
)

ENGINE_ROOT = Path(__file__).resolve().parents[3]  # apps/engine
CALLS_PER_MINUTE = 2.5  # 실제 LLM 확인 프롬프트의 추정치 — 2026-11-07 실측(호출 13회)과 말:곡 5:5 기준


def _clock(sec: float) -> str:
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


# ── 설정값 덮어쓰기 (--set, --sweep, --variant) ─────────────────────────
# 이름 앞머리로 대상을 가린다: chat.(config/chat_rules.yaml, 앞머리가 없으면 이것), engine.(EngineSettings),
# sim.(시나리오의 흉내 값), station.(시나리오의 방송 설정). 엔진 설정은 방송 동작을 바꾸는 수치만 연다.
# contract_version 2는 한 멘트에 입력 여러 건을 싣는다 (#267) — 흉내 백엔드는 두 버전을 모두 받는다
ENGINE_KEYS = {"target_buffer_sec": float, "max_concurrent_generations": int, "contract_version": int}
STATION_KEYS = ("intensity",)
# 회차의 루프 지연 P95가 이보다 크면 동시 실행이 타이밍 지표를 틀었다고 본다 [초기값] —
# 2026-11-10 실측(flood, 더미 LLM): 30분 방송 동시 60회의 180~200ms에서는 세그먼트 생성 P95가 그대로였고
# (12.21초 → 12.22초), 디스크 동기화를 켠 6분 방송 동시 30회의 1700ms에서는 20% 길어졌다 (ENGINE_TUNING_LOG T-001)
LAG_WARN_MS = 250.0
BASE = "base"
_VARIANT_NAME = re.compile(r"^[\w.=+-]+$")


@dataclass(frozen=True)
class Variant:
    """한 실행 안에서 견줄 설정 하나. base는 지금 설정(+ --set)이다."""

    name: str = BASE
    overrides: tuple[tuple[str, object], ...] = ()


BASE_VARIANT = Variant()


def parse_override(text: str) -> tuple[str, object]:
    """"reaction_window_sec=90" → ("chat.reaction_window_sec", 90). 값은 YAML로 읽는다."""
    key, sep, raw = text.partition("=")
    key = key.strip()
    if not sep or not key or not raw.strip():
        raise ValueError(f"'이름=값' 형식이 아니다: {text!r}")
    scope, _, name = key.rpartition(".")
    scope = scope or "chat"
    known = {"chat": ChatRules.model_fields, "engine": ENGINE_KEYS, "sim": SimSpec.model_fields,
             "station": STATION_KEYS}
    if scope not in known or name not in known[scope]:
        choices = ", ".join(sorted(known.get(scope, known)))
        raise ValueError(f"모르는 설정: {key} (쓸 수 있는 것: {choices})")
    return f"{scope}.{name}", yaml.safe_load(raw)


def build_variants(sets: list[str], sweeps: list[str], named: list[str]) -> list[Variant]:
    """--set은 모든 변형에 깔리고, --sweep의 값 하나와 --variant 하나가 각각 변형이 된다. base가 맨 앞이다."""
    common = tuple(parse_override(s) for s in sets)
    variants = [Variant(BASE, common)]
    for sweep in sweeps:
        key, sep, values = sweep.partition("=")
        if not sep or not values.strip():
            raise ValueError(f"'이름=값,값,…' 형식이 아니다: {sweep!r}")
        for value in values.split(","):
            pair = parse_override(f"{key}={value}")
            variants.append(Variant(f"{pair[0].split('.', 1)[1]}={value.strip()}", (*common, pair)))
    for item in named:
        name, sep, body = item.partition(":")
        if not sep or not _VARIANT_NAME.match(name):
            raise ValueError(f"'이름:설정=값,설정=값' 형식이 아니다: {item!r}")
        variants.append(Variant(name, (*common, *(parse_override(p) for p in body.split(",")))))
    names = [v.name for v in variants]
    if len(set(names)) != len(names):
        raise ValueError(f"변형 이름이 겹친다: {names}")
    for variant in variants:  # 30분을 돌린 뒤가 아니라 지금 틀린 값을 잡는다
        _apply_overrides(_blank_settings(), ScenarioSpec(name="check"), variant, None)
    return variants


def _blank_settings() -> EngineSettings:
    return EngineSettings(chat_rules_path=ENGINE_ROOT / "config" / "chat_rules.yaml")


def _apply_overrides(settings: EngineSettings, spec: ScenarioSpec, variant: Variant,
                     run_dir: Path | None) -> ScenarioSpec:
    """변형의 값을 엔진 설정과 시나리오에 넣는다. chat 값은 회차 폴더에 고친 chat_rules.yaml을 써서 넘긴다."""
    grouped: dict[str, dict[str, object]] = {"chat": {}, "engine": {}, "sim": {}, "station": {}}
    for key, value in variant.overrides:
        scope, name = key.split(".", 1)
        grouped[scope][name] = value
    if grouped["chat"]:
        rules = yaml.safe_load(Path(settings.chat_rules_path).read_text(encoding="utf-8"))
        rules.update(grouped["chat"])
        ChatRules.model_validate(rules)
        if run_dir is not None:
            path = run_dir / "chat_rules.yaml"
            path.write_text(yaml.safe_dump(rules, allow_unicode=True, sort_keys=False), encoding="utf-8")
            settings.chat_rules_path = path
    for name, value in grouped["engine"].items():
        setattr(settings, name, ENGINE_KEYS[name](value))
    if grouped["sim"]:
        spec = spec.model_copy(update={"sim": SimSpec.model_validate({**spec.sim.model_dump(), **grouped["sim"]})})
    if grouped["station"]:
        station = StationSpec.model_validate({**spec.station.model_dump(), **grouped["station"]})
        spec = spec.model_copy(update={"station": station})
    return spec


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
        emit_votes=True,  # 가짜 백엔드는 투표를 받는다 — 본 코너에 DJ 대 청취자가 있는 시나리오에서만 쓰인다
    )
    apply_llm_args(settings, args)
    return settings


async def _inject(spec: ScenarioSpec, backend: SimBackend, engine: StationEngine,
                  rng: random.Random, finished: asyncio.Event) -> None:
    """시각별 입력과 배경 채팅을 station.started 기준으로 보낸다."""
    await backend.started.wait()
    for story in spec.board_stories:  # 백엔드는 station.started 직후 후보를 보낸다 (#196)
        if story.at is None:
            engine.on_board_story({"story_id": f"story_sim_{story.id}", "body": story.body,
                                   "nickname": story.nickname, "posted_at": time.time() - 3600})
    events = [(item.at, item) for item in (*spec.inputs, *spec.controls)]
    events += [(story.at, story) for story in spec.board_stories if story.at is not None]
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
        if isinstance(item, StorySpec):  # 방송 중에 올라온 글 — 백엔드가 바로 후보로 보낸다
            rid = f"story_sim_{item.id}"
            backend.played.append(Played(backend.now(), "input", 0,
                                         text=f"게시판 사연 {rid} ({item.nickname or '익명'}): {item.body}"))
            engine.on_board_story({"story_id": rid, "body": item.body, "nickname": item.nickname,
                                   "posted_at": time.time()})
            continue
        if isinstance(item, tuple):  # 배경 채팅
            _, n, body, who = item
            rid = f"chatter_{n}"
            backend.played.append(Played(backend.now(), "input", 0, text=f"채팅 {rid} ({who}): {body}"))
            await engine.submit_request("chat", body, who, request_id=rid)
            continue
        if isinstance(item, ControlSpec) and item.type == "persona_switch":  # 테스트 화면의 페르소나 버튼
            persona = persona_payload(item, spec.station.voice if engine.settings.tts == "gemini" else None)
            backend.played.append(Played(backend.now(), "input", 0,
                                         text=f"조작 {item.id}: 페르소나 전환 → {persona['dj_name']}"))
            await backend.switch_persona(persona, item.id)
            continue
        if isinstance(item, ControlSpec) and item.type == "format_switch":  # 테스트 화면의 스테이션 버튼
            options = {key: value for key, value in (("format", item.format), ("corners", item.corners),
                                                     ("news_sections", item.news_sections)) if value is not None}
            backend.played.append(Played(backend.now(), "input", 0,
                                         text=f"조작 {item.id}: 형식 전환 → {_FORMATS[item.format]}"))
            await backend.switch_format(options, item.id)
            continue
        if isinstance(item, ControlSpec):  # 테스트 화면의 코너 버튼
            title = engine.telemetry.corner_titles.get(item.corner, item.corner)
            backend.played.append(Played(backend.now(), "input", 0, text=f"조작 {item.id}: 코너 변경 → {title}"))
            await backend.skip_corner(item.corner, item.id)
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
_FORMATS = {"talk": "토크", "music": "음악", "news": "뉴스"}


def fake_feeds(spec: ScenarioSpec, feeds_path: Path):
    """시나리오에 적은 기사(news)를 실제 피드 주소로 돌려주는 가짜 읽기 — 네트워크를 쓰지 않는다.

    분야의 피드 주소를 물으면 그 분야의 기사를 RSS로 만들어 준다. 읽는 데 sim.rss_latency_sec가 걸린다.
    """
    sections: dict[str, set[str]] = {}
    for code, entries in load_news_feeds(feeds_path).feeds.items():
        for entry in entries:
            sections.setdefault(entry.url, set()).add(code)

    def fetch(url: str) -> bytes:
        time.sleep(spec.sim.rss_latency_sec)  # 수집기가 스레드에서 부른다
        items = "".join(
            f"<item><title>{escape(n.title)}</title><description>{escape(n.summary or n.title)}</description>"
            f"<link>https://news.example.com/{i}</link></item>"
            for i, n in enumerate(spec.news) if n.section in sections.get(url, ()))
        return f"<rss><channel>{items}</channel></rss>".encode()

    return fetch
# persona 파일에 말투 선택지(style)가 없을 때 — 엔진은 방송에 쓰지 않고 스키마만 요구한다
_DEFAULT_STYLE = {"formality": "polite", "energy": "mid", "humor": "some"}


def persona_payload(control: ControlSpec, station_voice: str | None = None) -> dict:
    """persona 파일 → station.persona_changed의 persona (station.created와 같은 구조).

    보이스는 조작에 적은 값, 없으면 방송의 보이스다. 흉내 TTS는 보이스를 쓰지 않는다.
    """
    path = Path(control.persona)
    raw = yaml.safe_load((path if path.is_absolute() else ENGINE_ROOT / path).read_text(encoding="utf-8"))
    fields = ("dj_name", "concept", "tone", "examples", "signature_phrases", "forbidden")
    return {"persona_id": f"psn_sim_{control.id}", "style": raw.get("style") or _DEFAULT_STYLE,
            "voice": control.voice or station_voice or DEFAULT_GEMINI_VOICE,
            **{name: raw[name] for name in fields if name in raw}}


def _unsynced_metrics_db(path: Path) -> None:
    """회차의 계측 DB를 디스크 동기화 없이 미리 만든다.

    엔진은 기록마다 커밋하고 SQLite는 커밋마다 디스크를 기다린다. 방송 하나에서는 티가 안 나지만
    한 프로세스에서 수십 회차를 돌리면 그 대기가 이벤트 루프를 막아 모든 회차의 타이머가 밀린다
    (2026-11-10 실측: 동시 30회에서 루프 지연 P95 1.7초). 회차 DB는 끝나면 읽고 마는 파일이라 동기화가 필요 없다.
    """
    db = sqlite3.connect(str(path))
    try:
        db.execute("PRAGMA synchronous=OFF")
        db.executescript(telemetry_module._SCHEMA)
        db.commit()
    finally:
        db.close()


async def run_one(spec: ScenarioSpec, index: int, run_dir: Path, args: argparse.Namespace,
                  variant: Variant = BASE_VARIANT) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    settings = _settings(args, run_dir)
    spec = _apply_overrides(settings, spec, variant, run_dir)
    if spec.news:
        settings.rss_fetch = fake_feeds(spec, settings.news_feeds_path)
    seed = spec.sim.seed + index  # 변형이 달라도 같은 회차는 같은 seed다 — 짝지어 비교한다 (compare.py)
    rng = random.Random(seed)
    station_id = f"sim_{spec.name}_{index + 1}"
    st = spec.station
    profile_path = Path(args.persona or st.persona)
    profile_path = profile_path if profile_path.is_absolute() else ENGINE_ROOT / profile_path
    config = StationConfig(station_id=station_id, profile=load_profile(profile_path),
                           broadcast_minutes=args.minutes or st.broadcast_minutes, format=st.format,
                           corners=st.corners, intensity=st.intensity, topic=st.topic,
                           voice=st.voice if args.tts == "gemini" else None)
    backend = SimBackend(sim=spec.sim, tracks=spec.tracks, rng=random.Random(seed * 7919))
    _unsynced_metrics_db(settings.sqlite_path)
    engine = StationEngine(config, settings, backend)
    engine.telemetry._db.execute("PRAGMA synchronous=OFF")
    backend.engine = engine
    # 더미 LLM이면 실측에 맞춘 지연을 넣는다 — 파이프라인·사연 판정·갈래 계획이 같은 객체를 쓴다
    llm_delays = tune_dummy_llm(engine.scheduler.pipeline.llm, spec.sim, seed)
    sim_tts = None
    if args.tts == "sim":
        sim_tts = SimTts(spec.sim, random.Random(seed * 104729))
        engine.scheduler.pipeline.tts._inner = sim_tts  # 파이프라인과 접착 문구가 같은 CachedTts를 쓴다
        engine.tts_factory = lambda voice: sim_tts  # 페르소나 전환으로 보이스가 바뀌어도 흉내 TTS를 쓴다
    backend.queue_next_track()  # 백엔드는 방송 전에 첫 소개 곡을 정해 둔다

    finished = backend.finished
    started = time.time()

    async def broadcast():
        try:
            await engine.run()
        finally:
            finished.set()  # 엔진 계측이 닫혔다 — 가짜 백엔드는 더 알리지 않는다

    lag = LoopLag()
    tasks = [asyncio.create_task(broadcast()), asyncio.create_task(backend.play(finished)),
             asyncio.create_task(backend.report_backpressure(finished)),
             asyncio.create_task(_inject(spec, backend, engine, rng, finished)),
             asyncio.create_task(lag.watch(finished))]
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
        "llm_latency_sec": _mean(llm_delays) if llm_delays else None,  # 더미 LLM의 흉내 지연
        # 이 회차가 도는 동안 실행기의 타이머가 얼마나 늦게 깨어났나 — 크면 동시 실행이 지표를 틀었다
        "loop_lag_ms_p95": lag.percentile(95), "loop_lag_ms_max": lag.percentile(100),
        **_flow(backend.played),
        # 코너 변경 — 버튼부터 고른 코너의 첫 세그먼트 제출·재생·청취까지 (청취 = 재생 + 방송 지연)
        "corner_skips": [{**{k: v for k, v in skip.items() if k != "segment_id"},
                          "heard_sec": None if skip["play_sec"] is None
                          else round(skip["play_sec"] + spec.sim.broadcast_delay_sec, 1)}
                         for skip in backend.skips],
        # 페르소나 전환 — 버튼부터 새 DJ의 첫 세그먼트 제출·재생·청취까지. accepted가 거짓이면 엔진이 거절했다
        "persona_switches": [{**{k: v for k, v in switch.items() if k != "segment_id"},
                              "heard_sec": None if switch["play_sec"] is None
                              else round(switch["play_sec"] + spec.sim.broadcast_delay_sec, 1)}
                             for switch in backend.persona_switches],
        # 형식 전환 — 버튼부터 새 형식의 첫 세그먼트(corner) 제출·재생·청취까지
        "format_switches": [{**{k: v for k, v in change.items() if k not in ("segment_id", "applied_before")},
                             "heard_sec": None if change["play_sec"] is None
                             else round(change["play_sec"] + spec.sim.broadcast_delay_sec, 1)}
                            for change in backend.format_switches],
        # 투표 — 받은 vote.open마다 창을 연·닫은 시각과 표. rounds는 엔진이 결과를 받아 공개한 라운드 수다
        "votes": [{k: vote[k] for k in ("vote_id", "question", "options", "window_sec", "at", "opened_at",
                                        "closed_at", "counts", "discarded")} for vote in backend.votes],
        "vote_rounds": engine.sources.votes.rounds, "vote_score": dict(engine.sources.votes.score),
        "error": error,
    }
    reflected = _reflections(db, station_id)
    report["expect"] = _check(spec, backend, report, reflected)
    _write_run(run_dir, spec, index, seed, args, report, backend, reflected, db, station_id, variant)
    if args.tts == "sim":
        shutil.rmtree(settings.audio_dir, ignore_errors=True)  # 무음 파일 — 남길 이유가 없다
    return report


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _flow(played: list) -> dict:
    """말과 곡의 흐름 (#222) — 말 비율, 곡 사이 토크 길이, 곡 소개만 사이에 두고 곡이 이어진 횟수."""
    speech = sum(p.sec for p in played if p.kind == "speech")
    music = sum(p.sec for p in played if p.kind == "music")
    gaps: list[tuple[float, bool]] = []  # 곡과 곡 사이 — (말 길이, 곡 소개뿐인가)
    gap: list | None = None  # 첫 곡 전(오프닝)과 마지막 곡 뒤(엔딩)는 곡 사이가 아니다
    for p in sorted(played, key=lambda p: p.at):
        if p.kind == "music":
            if gap is not None:
                gaps.append((sum(s.sec for s in gap), all(s.corner_type == "music_intro" for s in gap)))
            gap = []
        elif p.kind == "speech" and gap is not None:
            gap.append(p)
    talk = sorted(sec for sec, _ in gaps)
    return {
        "speech_sec": round(speech, 1), "music_sec": round(music, 1),
        "talk_ratio": round(speech / (speech + music), 3) if speech + music else None,
        "talk_between_songs_p50": talk[(len(talk) - 1) // 2] if talk else None,
        "back_to_back_songs": sum(1 for _, intro_only in gaps if intro_only),
    }


def _reflections(db: Path, station_id: str) -> dict[str, tuple[str, float]]:
    """request_id → (세그먼트, 도착부터 재생 시작까지 초)."""
    rows = _read(db, "SELECT request_id, segment_id, quote_age FROM reflection_log WHERE station_id = ?",
                 (station_id,)) if db.exists() else []
    return {rid: (seg, age) for rid, seg, age in rows}


def _check(spec: ScenarioSpec, backend: SimBackend, report: dict,
           reflected: dict[str, tuple[str, float]]) -> list[dict]:
    aired = " ".join(p.script or "" for p in backend.played if p.kind == "speech")
    kinds = spec.request_kinds()
    out = []
    for e in spec.expect:
        if e.type == "reflected":
            story = f"story_sim_{e.input}"
            hit = reflected.get(story if story in kinds else f"in_{e.input}")
            passed = hit is not None and (e.within_sec is None or hit[1] <= e.within_sec)
            detail = "반영 안 됨" if hit is None else f"{hit[1]:.0f}초 뒤 재생"
        elif e.type == "not_reflected":  # 청취자 입력을 반영하지 않는 형식(뉴스)에서 온 입력
            story = f"story_sim_{e.input}"
            hit = reflected.get(story if story in kinds else f"in_{e.input}")
            passed, detail = hit is None, "반영 안 됨" if hit is None else f"{hit[1]:.0f}초 뒤 재생"
        elif e.type == "min_reflected":
            count = sum(1 for rid in reflected if kinds.get(rid, "chat") == e.kind)  # 나머지는 배경 채팅
            passed, detail = count >= e.value, f"{count}건 반영"
        elif e.type == "aired_contains":
            passed, detail = e.text in aired, ""
        elif e.type == "not_aired":
            passed, detail = e.text not in aired, ""
        elif e.type == "corner_entered":
            heard = next((s["heard_sec"] for s in report["sim"]["corner_skips"] if s["id"] == e.control), None)
            passed = heard is not None and (e.within_sec is None or heard <= e.within_sec)
            detail = "고른 코너에 들어가지 않음" if heard is None else f"{heard:.0f}초 뒤 들림"
        elif e.type == "persona_switched":
            # 버튼을 누르기 전에 방송이 끝났으면 기록이 없다
            switch = next((s for s in report["sim"]["persona_switches"] if s["id"] == e.control), None)
            heard = switch["heard_sec"] if switch else None
            passed = heard is not None and (e.within_sec is None or heard <= e.within_sec)
            detail = ("버튼 전에 방송이 끝남" if switch is None else "엔진이 거절함" if not switch["accepted"]
                      else "새 DJ의 멘트가 나가지 않음" if heard is None else f"{heard:.0f}초 뒤 들림")
        elif e.type == "format_switched":
            change = next((s for s in report["sim"]["format_switches"] if s["id"] == e.control), None)
            heard = change["heard_sec"] if change else None
            passed = heard is not None and (e.within_sec is None or heard <= e.within_sec)
            detail = ("버튼 전에 방송이 끝남" if change is None else "엔진이 거절함" if not change["accepted"]
                      else "새 형식의 멘트가 나가지 않음" if heard is None else f"{heard:.0f}초 뒤 들림")
        elif e.type == "min_vote_rounds":
            value = report["sim"]["vote_rounds"]
            passed, detail = value >= e.value, f"결과까지 공개한 투표 {value}번"
        elif e.type == "max_silence_sec":
            value = report["sim"]["silence_max_sec"]
            passed, detail = value <= e.value, f"최대 무음 {value}초"
        else:  # max_fallback
            value = report.get("generation", {}).get("outcomes", {}).get("fallback", 0)
            passed, detail = value <= e.value, f"대체 멘트 {value}건"
        out.append({"type": e.type, "target": e.input or e.control or e.kind or e.text or e.value, "note": e.note,
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
               report: dict, backend: SimBackend, reflected: dict, db: Path, station_id: str,
               variant: Variant = BASE_VARIANT) -> None:
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
             f"- 말 비율: {_cell(sim['talk_ratio'])} (말 {sim['speech_sec']}초, 곡 {sim['music_sec']}초), "
             f"곡 사이 토크 중앙값 {_cell(sim['talk_between_songs_p50'])}"
             f"{'초' if sim['talk_between_songs_p50'] is not None else ''}, "
             f"곡 소개만 두고 곡이 이어진 횟수 {sim['back_to_back_songs']}\n"
             f"- 실행 시간: {sim['wall_sec']}초\n- TTS 흉내 평균 지연: {sim['tts_latency_sec'] or '-'}초\n"
             + (f"- LLM 흉내 평균 지연: {sim['llm_latency_sec']}초 (더미 대본)\n" if sim["llm_latency_sec"] else ""))
    if sim["corner_skips"]:
        text += ("\n## 코너 변경\n\n청취는 재생에 방송 지연을 더한 값이다. 남은 분량은 버튼을 눌렀을 때 버리지 못하고 "
                 "그대로 나간 분량(재생 중인 것과 미리 인코딩한 것)이다.\n\n"
                 "| 시각 | 코너 | 버린 멘트 | 남은 분량 | 첫 멘트 제출 | 재생 | 청취 |\n|---|---|---|---|---|---|---|\n")
        text += "".join(
            f"| {_clock(s['at'])} | {titles.get(s['corner'], s['corner'])} | {s['discarded']}개 | {s['ahead_sec']}초 | "
            + " | ".join("-" if s[k] is None else f"{s[k]}초" for k in ("submit_sec", "play_sec", "heard_sec"))
            + " |\n" for s in sim["corner_skips"])
    if sim["persona_switches"]:
        text += ("\n## 페르소나 전환\n\n청취는 재생에 방송 지연을 더한 값이다. 남은 분량은 버튼을 눌렀을 때 버리지 못하고 "
                 "이전 DJ의 목소리로 그대로 나간 분량이다.\n\n"
                 "| 시각 | DJ | 버린 멘트 | 남은 분량 | 첫 멘트 제출 | 재생 | 청취 |\n|---|---|---|---|---|---|---|\n")
        text += "".join(
            f"| {_clock(s['at'])} | {s['dj_name']}{'' if s['accepted'] else ' (거절)'} | {s['discarded']}개 | "
            f"{s['ahead_sec']}초 | "
            + " | ".join("-" if s[k] is None else f"{s[k]}초" for k in ("submit_sec", "play_sec", "heard_sec"))
            + " |\n" for s in sim["persona_switches"])
    if sim["votes"]:
        score = sim["vote_score"]
        text += (f"\n## 투표\n\n결과까지 공개한 라운드 {sim['vote_rounds']}번 · 점수 DJ {score['dj']} : "
                 f"청취자 {score['listeners']} (무승부 {score['draw']})\n\n"
                 "| 알림 | 열림 | 닫힘 | 고민 | 표 |\n|---|---|---|---|---|\n")
        text += "".join(
            f"| {_clock(v['at'])} | {'버려짐' if v['discarded'] else '-' if v['opened_at'] is None else _clock(v['opened_at'])} | "
            f"{'-' if v['closed_at'] is None else _clock(v['closed_at'])} | {v['question']} | "
            + ("-" if v["counts"] is None else " / ".join(f"{k} {n}" for k, n in v["counts"].items()))
            + " |\n" for v in sim["votes"])
    if sim["format_switches"]:
        text += ("\n## 형식 전환\n\n청취는 재생에 방송 지연을 더한 값이다. 남은 분량은 버튼을 눌렀을 때 버리지 못하고 "
                 "이전 형식으로 그대로 나간 분량이다.\n\n"
                 "| 시각 | 형식 | 첫 코너 | 버린 멘트 | 남은 분량 | 첫 멘트 제출 | 재생 | 청취 |\n"
                 "|---|---|---|---|---|---|---|---|\n")
        text += "".join(
            f"| {_clock(s['at'])} | {_FORMATS[s['format']]}{'' if s['accepted'] else ' (거절)'} | "
            f"{titles.get(s['corner'], s['corner'] or '-')} | {s['discarded']}개 | {s['ahead_sec']}초 | "
            + " | ".join("-" if s[k] is None else f"{s[k]}초" for k in ("submit_sec", "play_sec", "heard_sec"))
            + " |\n" for s in sim["format_switches"])
    if variant.overrides:
        text += f"- 덮어쓴 설정 ({variant.name}): " + ", ".join(f"{k}={v}" for k, v in variant.overrides) + "\n"
    if sim["error"]:
        text += f"- **오류**: {sim['error']}\n"
    if report["expect"]:
        text += "\n## 기대 결과\n\n| 판정 | 종류 | 대상 | 설명 | 결과 |\n|---|---|---|---|---|\n"
        text += "".join(f"| {'통과' if e['passed'] else '**실패**'} | {e['type']} | {e['target']} | "
                        f"{e['note'] or ''} | {e['detail']} |\n" for e in report["expect"])
    (run_dir / "report.md").write_text(text, encoding="utf-8")
    (run_dir / "run.json").write_text(json.dumps({
        # 재현 정보 — 보고서 근거는 이 파일로 되짚는다. scenario에는 덮어쓴 흉내 값이 반영돼 있다
        "scenario": spec.model_dump(), "run": index + 1, "seed": seed, **provenance.snapshot(),
        "variant": variant.name, "overrides": dict(variant.overrides),
        "llm": _llm_meta(args),
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


def _llm_meta(args: argparse.Namespace) -> dict:
    kind = args.llm or "openai"
    return {"kind": kind, "model": None if kind == "dummy" else args.llm_model or "gpt-6-luna"}


# 지표 정의는 metrics.py에 있다 — 비교(compare.py)와 장부가 같은 목록을 쓴다
_SUMMARY_COLUMNS = [
    *((m.label, m.get) for m in METRICS if m.summary),
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
            lines.append(f"| {e.note or e.type} ({e.input or e.control or e.text or e.value}) "
                         f"| {passed}/{len(reports)} |")
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


def variant_root(out: Path, variant: Variant, variants: list[Variant]) -> Path:
    """변형이 하나면 예전 그대로 <out>/<시나리오>, 여럿이면 <out>/<변형>/<시나리오>."""
    return out / variant.name if len(variants) > 1 else out


async def run_all(specs: list[ScenarioSpec], args: argparse.Namespace, out: Path,
                  variants: list[Variant] | None = None) -> dict[str, dict[str, list[dict]]]:
    """변형 × 시나리오 × 회차를 한 번에 돌린다. 돌려주는 값은 {변형: {시나리오: [회차 리포트]}}."""
    variants = variants or [BASE_VARIANT]
    slots = asyncio.Semaphore(args.parallel)
    results: dict[str, dict[str, list[dict | None]]] = {
        v.name: {s.name: [None] * args.repeat for s in specs} for v in variants}

    launch = asyncio.Lock()

    async def one(variant: Variant, spec: ScenarioSpec, i: int) -> None:
        async with slots:
            async with launch:  # 한꺼번에 띄우면 방송 준비(접착 문구 렌더링)가 겹쳐 서로의 타이머를 민다
                await asyncio.sleep(args.stagger)
            tag = f"{spec.name} {i + 1}/{args.repeat}" + (f" [{variant.name}]" if len(variants) > 1 else "")
            print(f"시작 · {tag}", flush=True)
            run_dir = variant_root(out, variant, variants) / spec.name / f"run-{i + 1}"
            report = results[variant.name][spec.name][i] = await run_one(spec, i, run_dir, args, variant)
            sim = report["sim"]
            print(f"끝 · {tag} · 무음 {sim['silence_total_sec']}초"
                  + (f" · 오류 {sim['error']}" if sim["error"] else ""), flush=True)

    # 짝이 되는 회차(같은 시나리오·seed)를 붙여서 띄운다 — 변형끼리 같은 부하에서 돈다
    await asyncio.gather(*(one(v, s, i) for s in specs for i in range(args.repeat) for v in variants))
    for variant in variants:
        for spec in specs:
            write_summary(variant_root(out, variant, variants) / spec.name, spec, results[variant.name][spec.name])
    return results


def worst_lag(results: dict[str, dict[str, list[dict]]]) -> float | None:
    """회차별 루프 지연 P95 중 가장 큰 값 — LAG_WARN_MS를 넘으면 --parallel을 줄여 다시 돌린다."""
    lags = [r["sim"].get("loop_lag_ms_p95") for by_scenario in results.values()
            for reports in by_scenario.values() for r in reports]
    return max((lag for lag in lags if lag is not None), default=None)


def _iso(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at))


def write_session(out: Path, args: argparse.Namespace, specs: list[ScenarioSpec], variants: list[Variant],
                  results: dict[str, dict[str, list[dict]]], started: float, command: list[str]) -> None:
    """실행 한 번의 기록 — session.json(무엇을 어떻게 돌렸나), worktree.diff(커밋 안 한 변경),
    그리고 실행 폴더들 옆의 ledger.jsonl에 변형 × 시나리오마다 한 줄 (평균·표준편차)."""
    common = {"session": out.name, "started_at": _iso(started), **provenance.snapshot(),
              "llm": _llm_meta(args), "tts": args.tts, "persona": args.persona, "note": args.note}
    session = {**common, "finished_at": _iso(time.time()), "command": command, "repeat": args.repeat,
               "parallel": args.parallel, "minutes": args.minutes, "scenarios": [s.name for s in specs],
               "variants": {v.name: dict(v.overrides) for v in variants},
               "loop_lag_ms_p95_worst": worst_lag(results)}
    (out / "session.json").write_text(json.dumps(session, ensure_ascii=False, indent=2, default=str),
                                      encoding="utf-8")
    provenance.write_diff(out)
    with open(out.parent / "ledger.jsonl", "a", encoding="utf-8") as ledger:
        for variant in variants:
            for spec in specs:
                reports = results[variant.name][spec.name]
                clean = [r for r in reports if not r["sim"]["error"]]
                ledger.write(json.dumps({
                    **common, "variant": variant.name, "overrides": dict(variant.overrides),
                    "scenario": spec.name, "minutes": args.minutes or spec.station.broadcast_minutes,
                    "runs": len(clean), "errors": len(reports) - len(clean), "metrics": aggregate(clean),
                }, ensure_ascii=False, default=str) + "\n")


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onair-scenario", description="방송 시나리오 반복 실행 (실시간)")
    parser.add_argument("scenarios", nargs="+", type=Path, help="시나리오 파일 (config/scenarios/*.yaml)")
    parser.add_argument("--repeat", type=int, default=5, help="시나리오마다 회차 수")
    parser.add_argument("--parallel", type=int, default=10, help="동시에 돌릴 회차 수")
    parser.add_argument("--stagger", type=float, default=1.0, metavar="초",
                        help="회차를 이 간격으로 하나씩 띄운다 (기본 1초) — 준비 작업이 겹치지 않게 한다")
    parser.add_argument("--minutes", type=int, default=None, help="방송 길이를 덮어쓴다 (점검용)")
    parser.add_argument("--persona", default=None, help="persona 파일을 덮어쓴다 (페르소나 비교용)")
    parser.add_argument("--tts", choices=["sim", "gemini"], default="sim",
                        help="sim: 흉내(무료, 기본) / gemini: 실제 TTS (한도 사용)")
    parser.add_argument("--out", type=Path, default=ENGINE_ROOT / "var" / "scenario")
    parser.add_argument("--yes", action="store_true", help="실제 LLM·TTS 확인을 건너뛴다")
    tune = parser.add_argument_group(
        "설정값 비교", "이름은 chat_rules.yaml의 항목, 또는 engine.target_buffer_sec, sim.llm_quote_miss_rate처럼 쓴다. "
                      "지금 설정(base)과 변형을 같은 seed로 함께 돌리고 compare.md에 차이를 남긴다")
    tune.add_argument("--set", action="append", default=[], metavar="이름=값", dest="sets",
                      help="모든 회차에 적용할 값 (반복 지정)")
    tune.add_argument("--sweep", action="append", default=[], metavar="이름=값,값", dest="sweeps",
                      help="값마다 변형을 하나씩 만든다 (지금 값은 base로 따로 돈다)")
    tune.add_argument("--variant", action="append", default=[], metavar="이름:설정=값,설정=값", dest="variants",
                      help="여러 값을 함께 바꾼 변형 (반복 지정)")
    parser.add_argument("--note", default=None, help="이 실행의 목적 — session.json과 장부(ledger.jsonl)에 남는다")
    add_llm_args(parser)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    load_env()
    specs = [load_scenario(p) for p in args.scenarios]
    try:
        variants = build_variants(args.sets, args.sweeps, args.variants)
    except ValueError as exc:  # pydantic ValidationError도 ValueError다
        print(f"설정값 오류: {exc}", file=sys.stderr)
        return 1
    runs = len(specs) * args.repeat * len(variants)
    minutes = max(args.minutes or s.station.broadcast_minutes for s in specs)
    plan = f"{runs}회 실행 · 동시 {args.parallel}회 · 예상 시간 약 {-(-runs // args.parallel) * (minutes + 2)}분"
    if len(variants) > 1:
        plan += f" · 변형 {len(variants)}개 ({', '.join(v.name for v in variants)})"
    if (args.llm or "openai") != "dummy" or args.tts == "gemini":
        calls = (sum((args.minutes or s.station.broadcast_minutes) * CALLS_PER_MINUTE for s in specs)
                 * args.repeat * len(variants))
        print(f"{plan} · 예상 LLM 호출 약 {calls:.0f}회" + (" · 실제 TTS 사용" if args.tts == "gemini" else ""))
        if not args.yes:
            if not sys.stdin.isatty():
                print("실제 LLM·TTS를 쓰려면 --yes가 필요합니다", file=sys.stderr)
                return 1
            if input("진행할까요? [y/N] ").strip().lower() != "y":
                return 1
    else:
        print(plan)
    started = time.time()
    out = args.out / time.strftime("%Y%m%d-%H%M%S")
    results = asyncio.run(run_all(specs, args, out, variants))
    write_session(out, args, specs, variants, results, started,
                  list(argv) if argv is not None else sys.argv[1:])
    roots = [variant_root(out, v, variants) for v in variants]
    review.write(roots, out / "review.html", [v.name for v in variants] if len(variants) > 1 else None)
    print(f"결과: {out}\n대본 검토: {out / 'review.html'}")
    if table := compare.write_variants(out, [v.name for v in variants]):
        print(f"설정값 비교: {table}")
    lag = worst_lag(results)
    if lag is not None and lag > LAG_WARN_MS:
        print(f"경고: 실행기 루프 지연 P95가 {lag:.0f}ms입니다 ({LAG_WARN_MS:.0f}ms 초과). 동시에 도는 회차가 많아 "
              "생성 시간·반영 시간이 실제보다 길게 재졌을 수 있습니다 — --parallel을 줄여 다시 돌리세요.", file=sys.stderr)
    return 0
