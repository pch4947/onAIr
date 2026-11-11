"""시나리오 실행기 — 시나리오 파일, TTS 흉내, 가짜 백엔드, 1회 실행 (#213)."""
import argparse
import asyncio
import json
import random
from pathlib import Path

import pytest
from pydantic import ValidationError

from onair_engine.audio import measure_duration_ms
from onair_engine.domain import Prompt, SegmentKind, SegmentSubmission
from onair_engine.inputs.rules import load_chat_rules
from onair_engine.pipeline.llm import DummyLlmClient
from onair_engine.prompt_data import CHAT, data_block
from onair_engine.scenario import runner
from onair_engine.scenario.sim import Played, SimBackend, SimTts, tune_dummy_llm
from onair_engine.scenario.spec import ScenarioSpec, SimSpec, TrackSpec, load_scenario, parse_at

ROOT = Path(__file__).resolve().parent.parent


# ── 시나리오 파일 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("path", sorted((ROOT / "config" / "scenarios").glob("*.yaml")), ids=lambda p: p.stem)
def test_bundled_scenarios_load(path):
    assert load_scenario(path).name == path.stem


def test_holdout_scenarios_differ_from_the_tuning_scenarios():
    tuning = [load_scenario(p) for p in sorted((ROOT / "config" / "scenarios").glob("*.yaml"))]
    holdout = sorted((ROOT / "config" / "scenarios" / "holdout").glob("*.yaml"))
    assert holdout, "검증용 시나리오가 있어야 한다 (docs/ENGINE_TUNING.md 3.4절)"
    for path in holdout:
        spec = load_scenario(path)
        assert spec.name == path.stem and spec.name not in {s.name for s in tuning}
        assert spec.station.topic not in {s.station.topic for s in tuning}, "튜닝에 쓴 주제로는 검증이 안 된다"


def test_parse_at():
    assert parse_at("1:30") == 90 and parse_at(45) == 45 and parse_at("2") == 2


@pytest.mark.parametrize("bad", [
    {"inputs": [{"at": 1, "kind": "song_request", "who": "a"}]},  # 곡 정보 없음
    {"inputs": [{"at": 1, "kind": "chat", "who": "a", "body": " "}]},  # 빈 채팅
    {"inputs": [{"at": 1, "kind": "chat", "who": "a", "body": "x", "id": "d"},
                {"at": 2, "kind": "chat", "who": "b", "body": "y", "id": "d"}]},  # id 중복
    {"expect": [{"type": "reflected", "input": "none"}]},  # 없는 입력
    {"expect": [{"type": "not_aired"}]},  # text 없음
    {"station": {"format": "talk", "unknown": 1}},  # 모르는 필드
])
def test_invalid_scenarios_are_rejected(bad):
    with pytest.raises(ValidationError):
        ScenarioSpec.model_validate({"name": "x", **bad})


def test_inputs_get_default_ids():
    spec = ScenarioSpec.model_validate({"name": "x", "inputs": [{"at": "0:10", "kind": "chat", "who": "a", "body": "안녕"}]})
    assert spec.inputs[0].id == "i0" and spec.inputs[0].at == 10


# ── TTS 흉내 ───────────────────────────────────────────────────────────

def test_sim_tts_length_and_latency(tmp_path, monkeypatch):
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr("onair_engine.scenario.sim.asyncio.sleep", fake_sleep)
    tts = SimTts(SimSpec(tts_chars_per_sec=10, tts_jitter=0), random.Random(1))
    duration = asyncio.run(tts.synthesize("가" * 100, tmp_path / "a.wav"))
    assert duration == 10_000
    assert measure_duration_ms(tmp_path / "a.wav") == pytest.approx(10_000, abs=5), "방송 시간은 파일 길이를 잰다"
    assert slept == [pytest.approx(5.5 + 0.33 * 10)], "Gemini 실측 식: 4초 → 약 7초, 25초 → 약 14초"


def test_dummy_llm_gets_measured_latency_and_can_drop_quotes(monkeypatch):
    slept = []

    async def fake_sleep(sec):
        slept.append(sec)

    monkeypatch.setattr("onair_engine.pipeline.llm.asyncio.sleep", fake_sleep)
    llm = DummyLlmClient()
    delays = tune_dummy_llm(llm, SimSpec(llm_latency_sec=2, llm_jitter=0.5, llm_quote_miss_rate=1), seed=3)
    prompt = Prompt(system="", user=data_block(CHAT, "붕어빵은 팥이죠"))
    text = asyncio.run(llm.generate(prompt)).text
    assert slept == delays and 1 <= delays[0] <= 3, "지연은 실측 평균 주변에서 흔들린다"
    assert "붕어빵은 팥이죠" not in text, "인용 누락 흉내 — 실제 LLM은 넣으라는 채팅을 빠뜨리기도 한다"
    llm.quote_miss_rate = 0
    assert "붕어빵은 팥이죠" in asyncio.run(llm.generate(prompt)).text
    assert tune_dummy_llm(object(), SimSpec(), seed=1) is None, "실제 LLM은 건드리지 않는다"


# ── 설정값 덮어쓰기 ────────────────────────────────────────────────────

def test_overrides_parse_into_variants():
    variants = runner.build_variants(["flood_threshold=6"], ["reaction_window_sec=30,90"],
                                     ["tight:engine.target_buffer_sec=20,sim.llm_quote_miss_rate=0.5"])
    assert [v.name for v in variants] == ["base", "reaction_window_sec=30", "reaction_window_sec=90", "tight"]
    assert variants[0].overrides == (("chat.flood_threshold", 6),), "--set은 기준에도 깔린다"
    assert variants[2].overrides == (("chat.flood_threshold", 6), ("chat.reaction_window_sec", 90))
    assert dict(variants[3].overrides)["sim.llm_quote_miss_rate"] == 0.5


@pytest.mark.parametrize("sets, sweeps, named", [
    (["nope=1"], [], []),  # 없는 설정
    (["reaction_window_sec=0"], [], []),  # 범위 밖 — 30분을 돌리기 전에 잡는다
    (["engine.api_port=1"], [], []),  # 방송 동작과 무관한 엔진 설정은 열지 않는다
    (["flood_threshold"], [], []),  # 값 없음
    ([], ["reaction_window_sec="], []),
    ([], [], ["a b:flood_threshold=5"]),  # 폴더 이름으로 못 쓰는 변형 이름
    ([], ["flood_threshold=5"], ["flood_threshold=5:flood_threshold=6"]),  # 이름 겹침
])
def test_bad_overrides_are_rejected_before_running(sets, sweeps, named):
    with pytest.raises(ValueError):
        runner.build_variants(sets, sweeps, named)


def test_overrides_reach_settings_rules_and_sim(tmp_path):
    variant = runner.Variant("v", (("chat.reaction_window_sec", 90), ("engine.target_buffer_sec", 20),
                                   ("sim.llm_latency_sec", 0.5)))
    settings = runner._blank_settings()
    spec = runner._apply_overrides(settings, ScenarioSpec(name="x"), variant, tmp_path)
    assert settings.target_buffer_sec == 20.0 and spec.sim.llm_latency_sec == 0.5
    assert settings.chat_rules_path == tmp_path / "chat_rules.yaml", "고친 규칙 파일을 회차 폴더에 남긴다"
    rules = load_chat_rules(settings.chat_rules_path)
    assert rules.reaction_window_sec == 90
    assert rules.flood_threshold == load_chat_rules(ROOT / "config" / "chat_rules.yaml").flood_threshold


# ── 가짜 백엔드 ────────────────────────────────────────────────────────

class FakeEngine:
    def __init__(self):
        self.calls = []
        self.scheduler = argparse.Namespace(pipeline=argparse.Namespace(_pending_air={"s1": "첫 멘트"}))

    def __getattr__(self, name):
        if name.startswith("on_") or name == "report_backpressure":
            return lambda *a, **k: self.calls.append((name, a))
        raise AttributeError(name)


def _sub(sid, ms=200, music_ref=None):
    return SegmentSubmission(id=sid, station_id="st", audio_ref=f"st/{sid}.wav", duration_ms=ms,
                             corner_type="topic_talk", kind=SegmentKind.SPEECH, priority=0,
                             reorderable=False, music_ref=music_ref)


def _backend(**sim):
    backend = SimBackend(sim=SimSpec(broadcast_delay_sec=12, **sim),
                         tracks=[TrackSpec(title="곡1", artist="가수", sec=0.2),
                                 TrackSpec(title="곡2", artist="가수", sec=0.2)], rng=random.Random(1))
    backend.engine = FakeEngine()
    return backend


def test_backend_plays_in_order_reports_and_measures_silence():
    backend = _backend()

    async def scenario():
        backend.queue_next_track()
        await backend.notify_station("station.started", {})
        play = asyncio.create_task(backend.play(backend.finished))
        await backend.publish_segment(_sub("s1", music_ref="sim:1"))  # 곡 소개 → 곡을 튼다
        await asyncio.sleep(0.8)  # 대기열이 빈다 — 무음
        await backend.publish_segment(_sub("s2"))
        await asyncio.sleep(0.4)
        backend.finished.set()
        await play

    asyncio.run(scenario())
    kinds = [p.kind for p in backend.played]
    assert kinds == ["speech", "music", "silence", "speech"]
    assert backend.played[0].script == "첫 멘트"
    assert 0.2 < backend.played[2].sec < 0.6
    names = [c[0] for c in backend.engine.calls]
    assert names.count("on_track_queued") == 2, "소개된 곡을 가져가면 다음 곡을 알린다"
    published, started = [c for c in backend.engine.calls if c[0] in ("on_segment_published", "on_segment_started")][:2]
    assert started[1][1] - published[1][1] == pytest.approx(12, abs=0.05), "재생 시작 = 송출 + 방송 지연"
    assert "on_track_started" in names


def test_song_request_jumps_the_playlist():
    backend = _backend()
    backend.queue_next_track()
    backend.queue_next_track({"track": TrackSpec(title="신청곡", artist="가수", sec=0.2),
                              "request_id": "req1", "nickname": "눈사람"})
    payload = backend.engine.calls[-1][1][0]
    assert payload["source"] == "request" and payload["request_id"] == "req1"
    assert payload["track"]["title"] == "신청곡"
    backend.queue_next_track()
    assert backend.engine.calls[-1][1][0]["track"]["title"] == "곡1", "밀려난 곡이 다음 차례다"


def test_after_engine_finishes_the_queue_is_recorded_without_waiting():
    backend = _backend()

    async def scenario():
        await backend.notify_station("station.started", {})
        backend.finished.set()
        for i in range(3):
            await backend.publish_segment(_sub(f"s{i}", ms=60_000))
        await asyncio.wait_for(backend.play(backend.finished), timeout=2)

    asyncio.run(scenario())
    assert [round(p.at) for p in backend.played] == [0, 60, 120]
    assert not any(c[0] == "on_segment_published" for c in backend.engine.calls), "닫힌 계측에 쓰지 않는다"


def test_flow_metrics_count_talk_between_songs():
    def speech(at, sec, corner="topic_talk"):
        return Played(at, "speech", sec, corner_type=corner)

    played = [
        speech(0, 30, "opening"),  # 첫 곡 전 — 곡 사이가 아니다
        speech(30, 10, "music_intro"), Played(40, "music", 200),
        speech(240, 10, "music_intro"), Played(250, "music", 200),  # 곡 소개만 두고 곡이 이어졌다
        speech(450, 50), speech(500, 10, "music_intro"), Played(510, "music", 200),
        speech(710, 20, "closing"),  # 마지막 곡 뒤
    ]
    flow = runner._flow(played)
    assert flow["speech_sec"] == 130 and flow["music_sec"] == 600
    assert flow["talk_ratio"] == pytest.approx(130 / 730, abs=0.001)
    assert flow["talk_between_songs_p50"] == 10, "곡 사이 두 구간: 10초, 60초"
    assert flow["back_to_back_songs"] == 1


# ── 1회 실행 ───────────────────────────────────────────────────────────

def test_run_one_with_dummy_llm(tmp_path, monkeypatch):
    original = runner._settings

    def fast(args, run_dir):
        settings = original(args, run_dir)
        settings.target_buffer_sec = 1e9  # 송출을 기다리지 않고 끝까지 만든다 — 테스트 시간만 줄인다
        return settings

    monkeypatch.setattr(runner, "_settings", fast)
    spec = ScenarioSpec.model_validate({
        "name": "t", "station": {"topic": "겨울 간식", "broadcast_minutes": 1,
                                 "corners": ["topic_talk", "text_corner"]},
        "sim": {"tts_latency_base_sec": 0, "tts_latency_per_audio_sec": 0, "broadcast_delay_sec": 0},
        "tracks": [{"title": "곡", "artist": "가수", "sec": 5}],
        "inputs": [{"at": 0, "kind": "message", "who": "a", "nickname": "밤샘러", "body": "붕어빵 먹었어요", "id": "m"}],
        "expect": [{"type": "max_fallback", "value": 0}, {"type": "not_aired", "text": "절대없는문구"}],
    })
    args = argparse.Namespace(llm="dummy", llm_model=None, llm_base_url=None, llm_reasoning_effort=None,
                              minutes=None, persona=None, tts="sim", repeat=1, parallel=1, note="점검")
    variant = runner.Variant("fast", (("chat.flood_threshold", 5), ("sim.llm_latency_sec", 0.1)))
    report = asyncio.run(asyncio.wait_for(runner.run_one(spec, 0, tmp_path / "run-1", args, variant), timeout=120))
    assert report["sim"]["error"] is None
    assert report["generation"]["jobs"] > 0
    assert [e["passed"] for e in report["expect"]] == [True, True]
    for name in ("transcript.md", "report.md", "report.json", "run.json", "played.json"):
        assert (tmp_path / "run-1" / name).exists()
    played = json.loads((tmp_path / "run-1" / "played.json").read_text(encoding="utf-8"))["played"]
    first = next(p for p in played if p["kind"] == "speech")
    assert first["corner"] == "오프닝" and first["script"], "검토 페이지가 코너 이름과 대본을 쓴다"
    assert "**[오프닝]**" in (tmp_path / "run-1" / "transcript.md").read_text(encoding="utf-8")
    meta = json.loads((tmp_path / "run-1" / "run.json").read_text(encoding="utf-8"))
    assert meta["seed"] == 1 and meta["variant"] == "fast" and "git_dirty" in meta
    assert meta["overrides"] == {"chat.flood_threshold": 5, "sim.llm_latency_sec": 0.1}, "재현 정보에 덮어쓴 값이 남는다"
    assert meta["scenario"]["sim"]["llm_latency_sec"] == 0.1 and meta["llm"] == {"kind": "dummy", "model": None}
    assert load_chat_rules(tmp_path / "run-1" / "chat_rules.yaml").flood_threshold == 5
    assert report["sim"]["llm_latency_sec"] == pytest.approx(0.1, abs=0.05)
    assert not (tmp_path / "run-1" / "audio").exists(), "흉내 TTS의 무음 파일은 지운다"

    runner.write_summary(tmp_path, spec, [report, report])
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "| 1 |" in summary and "| 2 |" in summary and "최소 / 평균 / 최대" in summary
    assert "인용 누락률" in summary, "요약 표와 비교 표가 같은 지표 목록을 쓴다 (metrics.py)"
    assert (tmp_path / "issues.csv").exists()

    out = tmp_path / "session"
    out.mkdir()
    runner.write_session(out, args, [spec], [variant], {"fast": {"t": [report, report]}}, 0.0, ["t.yaml"])
    session = json.loads((out / "session.json").read_text(encoding="utf-8"))
    assert session["variants"] == {"fast": meta["overrides"]} and session["note"] == "점검"
    row = json.loads((tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert (row["session"], row["variant"], row["scenario"], row["runs"]) == ("session", "fast", "t", 2)
    assert row["metrics"]["fallback"]["mean"] == 0 and row["metrics"]["jobs"]["n"] == 2
