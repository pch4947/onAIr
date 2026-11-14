"""시나리오 실행기 — 시나리오 파일, TTS 흉내, 가짜 백엔드, 1회 실행 (#213)."""
import argparse
import asyncio
import dataclasses
import json
import random
from pathlib import Path

import pytest
from onair_schema import Persona
from pydantic import ValidationError

from onair_engine.audio import measure_duration_ms
from onair_engine.domain import Prompt, SegmentKind, SegmentSubmission
from onair_engine.inputs.rules import load_chat_rules
from onair_engine.pipeline.llm import DummyLlmClient
from onair_engine.prompt_data import CHAT, data_block
from onair_engine.scenario import runner
from onair_engine.scenario.sim import Played, SimBackend, SimTts, tune_dummy_llm
from onair_engine.scenario.spec import ScenarioSpec, SimSpec, TrackSpec, load_scenario, parse_at
from onair_engine.sources.rss import Feed, load_news_feeds, parse_feed

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
    {"controls": [{"at": 1, "corner": "text_corner", "id": "d"},
                  {"at": 2, "corner": "music_intro", "id": "d"}]},  # 조작 id 중복
    {"controls": [{"at": 1, "type": "persona", "corner": "text_corner"}]},  # 모르는 조작
    {"controls": [{"at": 1, "type": "persona_switch", "corner": "text_corner"}]},  # persona 없음
    {"controls": [{"at": 1}]},  # corner 없음
    {"expect": [{"type": "persona_switched"}]},  # control 없음
    {"controls": [{"at": 1, "type": "format_switch"}]},  # format 없음
    {"controls": [{"at": 1, "type": "format_switch", "format": "podcast"}]},  # 모르는 형식
    {"expect": [{"type": "not_reflected"}]},  # input 없음
    {"expect": [{"type": "min_vote_rounds"}]},  # value 없음
    {"sim": {"vote_people": -1}},
    {"news": [{"summary": "제목 없는 기사"}]},
    {"expect": [{"type": "corner_entered", "control": "none"}]},  # 없는 조작
    {"expect": [{"type": "corner_entered"}]},  # control 없음
    {"expect": [{"type": "min_reflected", "value": 1}]},  # kind 없음
    {"expect": [{"type": "min_reflected", "kind": "message"}]},  # value 없음
    {"inputs": [{"at": 1, "kind": "chat", "who": "a", "body": "x", "id": "d"}],
     "board_stories": [{"body": "y", "id": "d"}]},  # 입력과 사연의 id 중복
    {"station": {"format": "talk", "unknown": 1}},  # 모르는 필드
])
def test_invalid_scenarios_are_rejected(bad):
    with pytest.raises(ValidationError):
        ScenarioSpec.model_validate({"name": "x", **bad})


def test_inputs_get_default_ids():
    spec = ScenarioSpec.model_validate({"name": "x", "inputs": [{"at": "0:10", "kind": "chat", "who": "a", "body": "안녕"}]})
    assert spec.inputs[0].id == "i0" and spec.inputs[0].at == 10


def test_controls_get_default_ids_and_can_be_expected():
    spec = ScenarioSpec.model_validate({
        "name": "x", "controls": [{"at": "3:00", "corner": "text_corner"}],
        "expect": [{"type": "corner_entered", "control": "c0", "within_sec": 60}]})
    assert (spec.controls[0].id, spec.controls[0].at, spec.controls[0].type) == ("c0", 180, "corner_skip")


def test_expect_counts_reflected_inputs_by_kind_and_finds_board_stories():
    spec = ScenarioSpec.model_validate({
        "name": "x",
        "inputs": [{"at": 1, "kind": "message", "who": "a", "body": "문자", "id": "m"},
                   {"at": 2, "kind": "message", "who": "b", "body": "문자", "id": "n"}],
        "board_stories": [{"body": "사연", "id": "s"}, {"body": "사연", "at": "1:30"}],
        "expect": [{"type": "reflected", "input": "s"},
                   {"type": "min_reflected", "kind": "message", "value": 2},
                   {"type": "min_reflected", "kind": "board_story", "value": 1},
                   {"type": "min_reflected", "kind": "chat", "value": 2}],
    })
    assert spec.board_stories[1].id == "1" and spec.board_stories[1].at == 90
    backend = argparse.Namespace(played=[])
    reflected = {"in_m": ("seg", 1.0), "story_sim_s": ("seg", 1.0), "chatter_3": ("seg", 1.0)}
    results = runner._check(spec, backend, {"sim": {}}, reflected)
    assert [r["passed"] for r in results] == [True, False, True, False]
    assert results[1]["target"] == "message" and results[1]["detail"] == "1건 반영"


def test_board_stories_arrive_before_the_broadcast_and_during_it():
    spec = ScenarioSpec.model_validate({"name": "x", "board_stories": [
        {"body": "미리 쓴 글", "id": "early"}, {"body": "방송 중 글", "id": "live", "at": 0.05}]})
    backend = _backend()

    async def scenario():
        await backend.notify_station("station.started", {})
        await runner._inject(spec, backend, backend.engine, random.Random(1), backend.finished)

    asyncio.run(scenario())
    stories = [c[1][0] for c in backend.engine.calls if c[0] == "on_board_story"]
    assert [s["story_id"] for s in stories] == ["story_sim_early", "story_sim_live"]
    assert stories[0]["posted_at"] < stories[1]["posted_at"] - 3000
    assert [p.kind for p in backend.played] == ["input"], "방송 중 글은 대본에도 입력으로 남는다"


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
    backend = SimBackend(sim=SimSpec(**{"broadcast_delay_sec": 12, **sim}),
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


class SkippableEngine(FakeEngine):
    async def on_segments_discarded(self, payload):
        self.calls.append(("on_segments_discarded", (payload,)))


def test_corner_skip_discards_untouched_segments_and_times_the_new_corner():
    backend = _backend(buffer_low_sec=1.5)
    backend.engine = SkippableEngine()

    async def scenario():
        backend.queue_next_track()  # sim:1 곡1
        await backend.notify_station("station.started", {})
        for sid in ("s1", "s2", "s3"):
            await backend.publish_segment(_sub(sid, ms=1000))
        await backend.publish_segment(_sub("intro", ms=1000, music_ref="sim:1"))  # 다음 곡(sim:2 곡2)을 알린다
        before = len(backend.engine.calls)
        await backend.skip_corner("text_corner", "to_text")
        calls = backend.engine.calls[before:]
        await backend.publish_segment(_sub("old", ms=100))  # 다른 코너의 멘트는 세지 않는다
        first = dataclasses.replace(_sub("new", ms=100), corner_type="text_corner")
        await backend.publish_segment(first)
        await backend.publish_segment(dataclasses.replace(first, id="new2"))
        await backend._play_segment(first)
        return calls

    calls = asyncio.run(scenario())
    # 재생할 것이 없으면 미리 인코딩할 분량(1.5초)을 채울 때까지 앞의 둘은 그대로 나간다
    assert [(name, args[0]) for name, args in calls if name != "on_track_queued"] == [
        ("on_segments_discarded", {"segment_ids": ["s3", "intro"]}), ("on_corner_skip", {"corner": "text_corner"})]
    assert [name for name, _ in calls] == ["on_segments_discarded", "on_track_queued", "on_corner_skip"]
    requeued = next(args[0] for name, args in calls if name == "on_track_queued")
    assert requeued["track"]["track_ref"] == "sim:1", "소개가 버려진 곡을 다음 곡으로 다시 알린다"
    backend.queue_next_track()
    assert backend.engine.calls[-1][1][0]["track"]["title"] == "곡2", "그 뒤에 알렸던 곡이 다음 차례다"
    skip, = backend.skips
    assert (skip["id"], skip["discarded"], skip["ahead_sec"], skip["segment_id"]) == ("to_text", 2, 2.0, "new")
    assert skip["submit_sec"] is not None and skip["play_sec"] is not None


def test_persona_switch_discards_untouched_segments_and_times_the_new_dj():
    class Switching(SkippableEngine):
        """받아들이면 persona_id가 바뀐다 — 실제 엔진은 편성 루프의 다음 차례에 바꾼다."""

        def __init__(self):
            super().__init__()
            self.config = argparse.Namespace(persona_id="psn_old")

        async def on_persona_changed(self, payload):
            self.calls.append(("on_persona_changed", (payload,)))
            return payload["persona"]["dj_name"] != "거절"

    backend = _backend(buffer_low_sec=1.5)
    backend.engine = Switching()
    persona = {"persona_id": "psn_new", "dj_name": "번개"}

    async def scenario():
        await backend.notify_station("station.started", {})
        for sid in ("s1", "s2", "s3"):
            await backend.publish_segment(_sub(sid, ms=1000))
        await backend.switch_persona({"persona_id": "psn_bad", "dj_name": "거절"}, "bad")
        before = len(backend.engine.calls)
        await backend.publish_segment(_sub("s4", ms=1000))
        await backend.switch_persona(persona, "to_new")
        calls = backend.engine.calls[before:]
        await backend.publish_segment(_sub("old", ms=100))  # 엔진이 아직 바꾸지 않았다 — 이전 DJ의 멘트다
        backend.engine.config.persona_id = "psn_new"
        first = _sub("new", ms=100)
        await backend.publish_segment(first)
        await backend.publish_segment(_sub("new2", ms=100))
        await backend._play_segment(first)
        return calls

    calls = asyncio.run(scenario())
    assert calls == [("on_segments_discarded", ({"segment_ids": ["s4"]},)),
                     ("on_persona_changed", ({"persona": persona},))], "버린 멘트를 먼저 알린다"
    bad, switch = backend.persona_switches
    assert (bad["id"], bad["accepted"], bad["discarded"], bad["segment_id"]) == ("bad", False, 1, None)
    assert (switch["id"], switch["dj_name"], switch["accepted"], switch["segment_id"]) == ("to_new", "번개", True, "new")
    assert switch["submit_sec"] is not None and switch["play_sec"] is not None


def test_format_switch_discards_untouched_segments_and_times_the_new_format():
    class Switching(SkippableEngine):
        """받아들이면 편성 관리자가 형식을 바꾼 횟수가 는다 — 실제 엔진은 편성 루프의 다음 차례에 바꾼다."""

        def __init__(self):
            super().__init__()
            self.scheduler.format_switches = 0

        def on_format_changed(self, payload):
            self.calls.append(("on_format_changed", (payload,)))
            return payload["format"] != "talk"

    backend = _backend(buffer_low_sec=1.5)
    backend.engine = Switching()

    async def scenario():
        await backend.notify_station("station.started", {})
        for sid in ("s1", "s2", "s3"):
            await backend.publish_segment(_sub(sid, ms=1000))
        await backend.switch_format({"format": "talk"}, "same")
        before = len(backend.engine.calls)
        await backend.publish_segment(_sub("s4", ms=1000))
        await backend.switch_format({"format": "news", "news_sections": ["it"]}, "to_news")
        calls = backend.engine.calls[before:]
        await backend.publish_segment(_sub("old", ms=100))  # 엔진이 아직 바꾸지 않았다 — 이전 형식의 멘트다
        backend.engine.scheduler.format_switches = 1
        first = dataclasses.replace(_sub("new", ms=100), corner_type="headline")
        await backend.publish_segment(first)
        await backend.publish_segment(_sub("new2", ms=100))
        await backend._play_segment(first)
        return calls

    calls = asyncio.run(scenario())
    assert calls == [("on_segments_discarded", ({"segment_ids": ["s4"]},)),
                     ("on_format_changed", ({"format": "news", "news_sections": ["it"]},))], "버린 멘트를 먼저 알린다"
    same, change = backend.format_switches
    assert (same["id"], same["accepted"], same["segment_id"]) == ("same", False, None)
    assert (change["id"], change["format"], change["accepted"], change["segment_id"], change["corner"]) == (
        "to_news", "news", True, "new", "headline")
    assert change["submit_sec"] is not None and change["play_sec"] is not None


def test_backend_opens_a_vote_when_its_segment_plays_and_reports_the_count():
    backend = _backend(buffer_low_sec=1.5, vote_people=10, broadcast_delay_sec=0.02)
    backend.engine = SkippableEngine()
    vote = {"vote_id": "vote_1", "kind": "game", "question": "머리부터? 꼬리부터?", "options": ["머리부터", "꼬리부터"],
            "segment_id": "ask", "window_sec": 0.05}

    async def scenario():
        await backend.notify_station("station.started", {})
        await backend.notify_station("vote.open", vote)
        await backend.notify_station("vote.open", {**vote, "vote_id": "vote_2", "segment_id": "gone"})
        await backend.publish_segment(_sub("s1", ms=1000))
        await backend.publish_segment(_sub("s2", ms=1000))
        await backend.publish_segment(_sub("gone", ms=1000))
        await backend.skip_corner("text_corner")  # gone이 버려진다 — 그 투표는 열리지 않는다
        assert backend.votes[0]["opened_at"] is None, "기준 세그먼트가 재생되기 전에는 열지 않는다"
        await backend._play_segment(_sub("ask", ms=10))
        await backend._play_segment(_sub("gone", ms=10))
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    opened, dropped = backend.votes
    assert opened["opened_at"] is not None and opened["closed_at"] is not None
    assert sum(opened["counts"].values()) == 10 and set(opened["counts"]) == {"머리부터", "꼬리부터"}
    assert dropped["discarded"] and dropped["opened_at"] is None and dropped["counts"] is None
    results = [args[0] for name, args in backend.engine.calls if name == "on_vote_result"]
    assert results == [{"vote_id": "vote_1", "counts": opened["counts"], "total": 10}]
    assert any(p.kind == "input" and p.text.startswith("투표 열림 vote_1") for p in backend.played)


def test_fake_feeds_serve_the_scenario_news_by_section():
    spec = ScenarioSpec.model_validate({"name": "x", "sim": {"rss_latency_sec": 0}, "news": [
        {"title": "반도체 설계 & 검증 기술 공개", "summary": "대기 전력 절반", "section": "it"},
        {"title": "전통시장 방문객 늘어", "section": "economy"},
    ]})
    feeds = load_news_feeds(ROOT / "config" / "news_feeds.yaml")
    fetch = runner.fake_feeds(spec, ROOT / "config" / "news_feeds.yaml")
    it = parse_feed(fetch(feeds.feeds["it"][0].url), Feed("매체", "u", "IT"))
    assert [(item.title, item.summary, item.section) for item in it] == [("반도체 설계 & 검증 기술 공개", "대기 전력 절반", "IT")]
    economy = parse_feed(fetch(feeds.feeds["economy"][0].url), Feed("매체", "u", "경제"))
    assert [item.title for item in economy] == ["전통시장 방문객 늘어"]
    assert parse_feed(fetch(feeds.feeds["sports"][0].url), Feed("매체", "u")) == []


def test_persona_payload_fills_what_a_persona_file_lacks():
    control = ScenarioSpec.model_validate({"name": "x", "controls": [
        {"at": 1, "type": "persona_switch", "persona": "config/personas/saebyeok.yaml", "id": "a"},
        {"at": 2, "type": "persona_switch", "persona": "config/personas/beongae.yaml", "voice": "puck", "id": "b"},
    ]}).controls
    plain = Persona.model_validate(runner.persona_payload(control[0], "kore"))
    assert (plain.persona_id, plain.dj_name, plain.voice, plain.style.formality) == ("psn_sim_a", "새벽", "kore", "polite")
    styled = Persona.model_validate(runner.persona_payload(control[1], "kore"))
    assert (styled.dj_name, styled.voice, styled.style.formality) == ("번개", "puck", "casual")
    assert Persona.model_validate(runner.persona_payload(control[1])).voice, "흉내 TTS에서도 스키마를 채운다"


def test_corner_skip_keeps_everything_when_little_is_queued():
    backend = _backend(buffer_low_sec=15)
    backend.engine = SkippableEngine()

    async def scenario():
        await backend.notify_station("station.started", {})
        await backend.publish_segment(_sub("s1", ms=1000))
        await backend.skip_corner("text_corner")

    asyncio.run(scenario())
    assert [name for name, _ in backend.engine.calls] == ["on_corner_skip"]
    assert backend.skips[0]["discarded"] == 0 and backend.skips[0]["play_sec"] is None


def test_displaced_song_requests_are_kept_and_newest_goes_first():
    backend = _backend()

    def request(name):
        return {"track": TrackSpec(title=name, artist="가수", sec=0.2), "request_id": name, "nickname": name}

    def queued():
        return backend.engine.calls[-1][1][0]

    async def intro_taken():
        await backend.publish_segment(_sub("s", music_ref=queued()["track"]["track_ref"]))

    backend.queue_next_track()
    for name in ("첫째", "둘째", "셋째"):
        backend.queue_next_track(request(name))
    order = [queued()["request_id"]]
    for _ in range(2):
        asyncio.run(intro_taken())  # 소개를 가져가면 밀린 신청곡부터
        order.append(queued()["request_id"])
    assert order == ["셋째", "둘째", "첫째"], "실제 백엔드처럼 최근 신청부터, 밀린 신청도 잃지 않는다"
    asyncio.run(intro_taken())
    assert queued()["track"]["title"] == "곡1" and "request_id" not in queued(), "신청곡이 다 나가면 플레이리스트"


def test_displaced_track_already_being_introduced_is_not_played_twice():
    backend = _backend()

    def request(name):
        return {"track": TrackSpec(title=name, artist="가수", sec=0.2), "request_id": name, "nickname": name}

    def queued():
        return backend.engine.calls[-1][1][0]

    async def scenario():
        backend.queue_next_track()  # sim:1 곡1 — 엔진이 소개를 만들기 시작한다
        backend.queue_next_track(request("첫째"))  # sim:2 — 곡1은 밀려난다
        await backend.publish_segment(_sub("late", music_ref="sim:1"))  # 만들던 곡1 소개가 온다
        assert queued()["request_id"] == "첫째", "다음 곡은 그대로 신청곡이다"
        backend.queue_next_track(request("둘째"))  # sim:3 — 첫째가 밀려난다
        await backend.publish_segment(_sub("late2", music_ref="sim:2"))  # 만들던 첫째 소개가 온다
        order = []
        for sid in ("a", "b", "c"):
            await backend.publish_segment(_sub(sid, music_ref=queued()["track"]["track_ref"]))
            order.append(queued()["track"]["title"])
        return order

    assert asyncio.run(scenario()) == ["곡2", "곡1", "곡2"], "이미 소개된 곡1과 첫째는 다시 나가지 않는다"
    assert len(backend._playlist) == 2 and not backend._requests


def test_corner_skip_keeps_song_requests_whose_intro_was_discarded():
    backend = _backend(buffer_low_sec=0.5)
    backend.engine = SkippableEngine()

    def request(name):
        return {"track": TrackSpec(title=name, artist="가수", sec=0.2), "request_id": name, "nickname": name}

    def queued():
        return next(args[0] for name, args in reversed(backend.engine.calls) if name == "on_track_queued")

    async def scenario():
        backend.queue_next_track(request("첫째"))  # sim:1
        await backend.notify_station("station.started", {})
        await backend.publish_segment(_sub("s1", ms=1000))
        await backend.publish_segment(_sub("intro", ms=1000, music_ref="sim:1"))  # 다음 곡은 플레이리스트
        backend.queue_next_track(request("둘째"))  # 소개가 대기열에 있는 동안 새 신청곡
        await backend.skip_corner("text_corner")  # 첫째의 소개가 버려진다
        order = [queued()["request_id"]]
        await backend.publish_segment(_sub("intro2", music_ref=queued()["track"]["track_ref"]))
        order.append(queued()["request_id"])
        await backend.publish_segment(_sub("intro3", music_ref=queued()["track"]["track_ref"]))
        return order

    assert asyncio.run(scenario()) == ["첫째", "둘째"], "소개가 버려진 신청곡을 다시 알리고, 그 뒤 신청곡도 남는다"
    assert queued()["track"]["title"] == "곡1" and "request_id" not in queued()


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
        "controls": [{"at": 1, "corner": "text_corner", "id": "skip"},
                     {"at": 0.5, "type": "persona_switch", "persona": "config/personas/beongae.yaml", "id": "dj"},
                     {"at": 0.2, "type": "format_switch", "format": "music", "id": "fmt"},
                     {"at": 0.4, "type": "format_switch", "format": "talk", "corners": ["text_corner", "topic_talk"],
                      "id": "back"}],
        "expect": [{"type": "max_fallback", "value": 0}, {"type": "not_aired", "text": "절대없는문구"},
                   {"type": "corner_entered", "control": "skip"}, {"type": "persona_switched", "control": "dj"},
                   {"type": "format_switched", "control": "back"}, {"type": "not_reflected", "input": "m"}],
    })
    args = argparse.Namespace(llm="dummy", llm_model=None, llm_base_url=None, llm_reasoning_effort=None,
                              minutes=None, persona=None, tts="sim", repeat=1, parallel=1, note="점검")
    variant = runner.Variant("fast", (("chat.flood_threshold", 5), ("sim.llm_latency_sec", 0.1)))
    report = asyncio.run(asyncio.wait_for(runner.run_one(spec, 0, tmp_path / "run-1", args, variant), timeout=120))
    assert report["sim"]["error"] is None
    assert report["generation"]["jobs"] > 0
    assert [e["passed"] for e in report["expect"]][:2] == [True, True]
    # 1분 방송이라 버튼이 엔딩 뒤에 올 수도 있다 — 기록과 판정이 남는지만 본다
    skip, = report["sim"]["corner_skips"]
    assert skip["id"] == "skip" and report["expect"][2]["passed"] == (skip["heard_sec"] is not None)
    assert "## 코너 변경" in (tmp_path / "run-1" / "report.md").read_text(encoding="utf-8")
    switch, = report["sim"]["persona_switches"]
    assert (switch["id"], switch["dj_name"], switch["accepted"]) == ("dj", "번개", True)
    assert report["expect"][3]["passed"] == (switch["heard_sec"] is not None)
    assert "## 페르소나 전환" in (tmp_path / "run-1" / "report.md").read_text(encoding="utf-8")
    changes = report["sim"]["format_switches"]
    assert [(c["id"], c["format"], c["accepted"]) for c in changes] == [("fmt", "music", True), ("back", "talk", True)]
    assert report["expect"][4]["passed"] == (changes[1]["heard_sec"] is not None)
    assert report["expect"][5]["type"] == "not_reflected"
    assert "## 형식 전환" in (tmp_path / "run-1" / "report.md").read_text(encoding="utf-8")
    transcript = (tmp_path / "run-1" / "transcript.md").read_text(encoding="utf-8")
    assert "조작 dj: 페르소나 전환 → 번개" in transcript
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
