"""DJ 대 청취자 코너 (#279) — 고민 만들기, 투표 라운드의 상태, vote.open / vote.result, 결과 대기와 코너가 끊길 때."""
import asyncio
import json
import time
from pathlib import Path

import pytest

from onair_engine.corners import STEP_KINDS, build_prompt
from onair_engine.corners.run import CornerRun, MaterialSources, Thread, TrackSource
from onair_engine.domain import Material, Prompt, StationConfig, StationProfile
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.format import ClockEntry, load_format
from onair_engine.inputs.dilemmas import (
    MARKER,
    Dilemma,
    DilemmaPlan,
    dummy_dilemmas_json,
    parse_dilemmas,
)
from onair_engine.inputs.dilemmas import build_prompt as dilemma_prompt
from onair_engine.inputs.votes import LATE, MATCH, MISMATCH, NO_VOTES, PENDING, TIE, VoteBoard, view
from onair_engine.manager import EngineManager
from onair_engine.pipeline.llm import DummyLlmClient
from onair_engine.pipeline.safety import SafetyChecker
from onair_engine.sources.rss import RssCollector
from test_corner_skip import _summaries, _until

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "config" / "safety_rules.yaml"
PROFILE = StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송")
BUNG = Dilemma("붕어빵은 머리부터 먹을까요, 꼬리부터 먹을까요?", ("머리부터", "꼬리부터"), 1, "바삭한 쪽이 좋아서요.")
HOT = Dilemma("추운 날 간식은 따뜻한 게 좋을까요?", ("따뜻한 것", "차가운 것"), 0, "손부터 녹이고 싶어서요.")


# ── 고민 만들기 ────────────────────────────────────────────────────────

def test_dilemma_prompt_carries_the_topic_as_data_and_the_dj():
    prompt = dilemma_prompt("겨울 간식", 3, PROFILE)
    assert MARKER in prompt.user and "고민 3개" in prompt.user
    assert '<data label="지금 화제">\n겨울 간식\n</data>' in prompt.user
    assert "테스트" in prompt.user and "차분한 존댓말" in prompt.user


def test_parse_dilemmas_keeps_only_two_way_choices_with_a_pick():
    text = json.dumps({"dilemmas": [
        {"question": "붕어빵은 머리부터?", "options": ["머리부터", "꼬리부터"], "pick": 1, "reason": "바삭해서요."},
        {"question": "문자열 번호", "options": ["가", "나"], "pick": "0", "reason": ""},
        {"question": "선택지 셋", "options": ["가", "나", "다"], "pick": 0},
        {"question": "같은 선택지", "options": ["가", "가"], "pick": 0},
        {"question": "선택 없음", "options": ["가", "나"]},
        {"question": "범위 밖", "options": ["가", "나"], "pick": 2},
        {"options": ["가", "나"], "pick": 0},
        "고민 아님",
    ]}, ensure_ascii=False)
    kept = parse_dilemmas("설명 " + text)
    assert [(d.question, d.options, d.pick) for d in kept] == [
        ("붕어빵은 머리부터?", ("머리부터", "꼬리부터"), 1), ("문자열 번호", ("가", "나"), 0)]
    for bad in ("없음", '{"dilemmas": "x"}', '{"dilemmas": [{"question": "q", "options": ["a"], "pick": 0}]}'):
        with pytest.raises(ValueError):
            parse_dilemmas(bad)


def test_dilemma_plan_hands_over_safe_dilemmas_and_survives_failure(tmp_path):
    class Fails(DummyLlmClient):
        async def complete(self, prompt: Prompt, *, max_tokens: int) -> str:
            return "고민을 만들 수 없습니다"

    safety = SafetyChecker(RULES)
    got = []
    asyncio.run(DilemmaPlan(DummyLlmClient(delay_sec=0), "겨울 간식", PROFILE, count=3,
                            violates=safety.check_l2, on_ready=got.extend).run())
    assert len(got) == 3 and all(d.pick in (0, 1) and len(d.options) == 2 for d in got)
    assert len(json.loads(dummy_dilemmas_json(dilemma_prompt("주제", 9, PROFILE)))["dilemmas"]) == 5
    failed = []
    asyncio.run(DilemmaPlan(Fails(delay_sec=0), "겨울 간식", PROFILE, count=3,
                            violates=safety.check_l2, on_ready=failed.extend).run())
    assert failed == [], "실패하면 고민이 없어 코너가 대체된다"


# ── 라운드 상태 ────────────────────────────────────────────────────────

def test_round_outcome_and_score():
    board = VoteBoard()
    board.add([BUNG, HOT, BUNG, HOT, BUNG])
    cases = [({"머리부터": 3, "꼬리부터": 9}, MATCH), ({"따뜻한 것": 2, "차가운 것": 8}, MISMATCH),
             ({"머리부터": 5, "꼬리부터": 5}, TIE), ({"따뜻한 것": 0, "차가운 것": 0}, NO_VOTES), (None, LATE)]
    for counts, outcome in cases:
        round_ = board.start()
        assert board.submitted(round_, "seg", 100.0)
        if counts is not None:
            assert board.result(round_.vote_id, counts) is round_
            assert board.result(round_.vote_id, {"머리부터": 99}) is None, "재전달은 받지 않는다"
        assert round_.outcome == outcome
        assert board.settle() is round_ and board.active is None
    assert board.score == {"dj": 1, "listeners": 1, "draw": 2} and board.rounds == 4, "결과를 못 받은 라운드는 세지 않는다"
    assert board.result("vote_gone", {"머리부터": 1}) is None and board.count() == 0


def test_waiting_follows_the_window_from_the_start_of_the_asking_segment():
    board = VoteBoard(window_sec=60, grace_sec=10, lead_sec=120)
    board.add([BUNG])
    round_ = board.start()
    assert board.waiting(0.0), "고민 멘트를 만드는 동안"
    board.submitted(round_, "seg_ask", 100.0)
    assert board.waiting(100.0 + 120 + 69) and not board.waiting(100.0 + 120 + 71), "재생 시작을 모르면 넉넉히 기다린다"
    board.started("seg_other", 110.0)
    assert round_.opened_at is None
    board.started("seg_ask", 130.0)
    assert board.waiting(199.0) and not board.waiting(201.0), "창이 닫히고 10초까지"
    board.result(round_.vote_id, {"머리부터": 1, "꼬리부터": 0})
    assert not board.waiting(150.0), "결과가 오면 더 기다리지 않는다"


def test_waiting_talk_follows_the_window_as_it_will_be_when_the_talk_airs():
    board = VoteBoard(window_sec=60, grace_sec=10)
    board.add([BUNG])
    round_ = board.start()
    assert board.next_talk(0.0) == "open", "고민 멘트를 만드는 동안"
    board.submitted(round_, "seg_ask", 100.0)
    board.queued_until = 500.0
    assert board.next_talk(100.0) == "open", "창이 열리기 전에는 닫히는 시각을 모른다"
    board.started("seg_ask", 110.0)  # 110초에 열려 170초에 닫힌다
    board.queued_until = 130.0
    assert board.next_talk(120.0) == "open"
    board.queued_until = 146.0  # 닫히기 25초 전부터 나갈 멘트는 마감을 알린다
    assert board.next_talk(125.0) == "closing"
    round_.closing_said = True
    board.queued_until = 160.0
    assert board.next_talk(140.0) == "closed", "마감은 한 번만 알린다"
    board.queued_until = 186.0  # 닫히고 15초 뒤까지 — 결과 공개까지 이을 만큼 만들어 뒀다
    assert board.next_talk(150.0) is None
    board.queued_until = 0.0
    assert board.next_talk(179.0) == "closed" and board.next_talk(181.0) is None, "대기 상한(닫히고 10초)이 지났다"
    board.result(round_.vote_id, {"머리부터": 1, "꼬리부터": 2})
    assert board.next_talk(150.0) is None, "결과가 오면 더 잇지 않는다"


def test_abandoned_and_discarded_rounds():
    board = VoteBoard()
    board.add([BUNG, HOT])
    first = board.start()
    board.abandon()
    assert board.active is None and board.count() == 2, "제출 전이면 고민을 되돌린다"
    again = board.start()
    assert again.dilemma is BUNG and again is not first
    assert not board.submitted(first, "seg", 1.0), "버려진 라운드는 투표를 열지 않는다"
    board.submitted(again, "seg_ask", 1.0)
    assert board.discarded("seg_ask") and board.count() == 2, "기준 세그먼트가 재생 전에 버려지면 고민을 되돌린다"
    opened = board.start()
    board.submitted(opened, "seg_ask2", 2.0)
    board.started("seg_ask2", 3.0)
    assert not board.discarded("seg_ask2"), "이미 열린 투표는 버려진 것이 아니다"
    board.abandon()
    assert board.count() == 1, "방송에 나간 고민은 되돌리지 않는다"
    assert board.result(opened.vote_id, {"머리부터": 1, "꼬리부터": 0}) is None, "버린 투표의 결과는 받지 않는다"
    rejected = board.start()
    board.abandon(requeue=False)
    assert board.count() == 0 and rejected.dilemma is HOT


# ── 단계 진행과 프롬프트 ──────────────────────────────────────────────

def _game_run() -> tuple[CornerRun, VoteBoard]:
    fmt = load_format("talk", ROOT / "config" / "formats")
    sources = MaterialSources(tracks=TrackSource(), rss=RssCollector())
    sources.votes.add([BUNG, HOT])
    run = CornerRun(ClockEntry(fmt.corner("dj_vs_listeners")), sources=sources, intensity=fmt.intensity["mid"],
                    thread=Thread())
    return run, sources.votes


def test_round_steps_wait_for_the_result_then_reveal():
    run, board = _game_run()
    assert run.next_step().step.glue == "call"
    ask = run.next_step()
    round_ = ask.items[0]["round"]
    assert ask.step.kind == "game_ask" and round_.dilemma is BUNG
    board.submitted(round_, "seg_ask", time.time())
    board.started("seg_ask", time.time())
    talks = [run.next_step() for _ in range(20)]
    assert [t.step.kind for t in talks] == ["game_talk"] * 20 and round_.talks == 20, "결과가 올 때까지 잇는다"
    assert run.next_step() is PENDING and run.next_step() is PENDING, "상한까지 했는데 결과가 아직이면 기다린다"
    assert round_.phase == "open", "만들어 둔 분량이 없으면 지금 시각으로 본다 — 창은 방금 열렸다"
    board.result(round_.vote_id, {"머리부터": 2, "꼬리부터": 10})
    reveal = run.next_step()
    assert reveal.step.kind == "game_reveal" and reveal.items[0]["round"] is round_
    board.settle()
    second = run.next_step()
    assert second.step.kind == "game_ask" and second.items[0]["round"].dilemma is HOT
    board.result(second.items[0]["round"].vote_id, {"따뜻한 것": 1, "차가운 것": 0})
    assert run.next_step().step.kind == "game_reveal", "결과가 먼저 오면 이야기 없이 공개한다"
    board.settle()
    assert [run.next_step().step.kind for _ in range(1)] == ["game_final"], "고민이 떨어지면 남은 라운드는 건너뛴다"
    assert run.next_step().step.glue == "close" and run.next_step() is None


def test_open_round_is_finished_even_when_the_corner_is_over_budget():
    run, board = _game_run()
    run.next_step()
    round_ = run.next_step().items[0]["round"]
    board.submitted(round_, "seg_ask", time.time())
    run.spent_ms = run.spec.budget_sec[1] * 1000 + 1
    assert run.next_step().step.kind == "game_talk", "열어 둔 투표는 결과 공개까지 간다"
    board.result(round_.vote_id, {"머리부터": 1, "꼬리부터": 4})
    assert run.next_step().step.kind == "game_reveal"
    board.settle()
    assert run.next_step().step.kind == "game_final", "예산을 넘겼으니 새 고민은 꺼내지 않는다"
    assert board.count() == 1


def test_prompts_hide_the_pick_until_the_reveal_and_keep_numbers_as_data():
    board = VoteBoard()
    board.add([BUNG])
    round_ = board.start()
    ask = build_prompt("game_ask", Material(items=[view(round_, board.score)]), PROFILE).user
    assert "머리부터 / 꼬리부터" in ask and "아직 밝히지 않고" in ask and "바삭한" not in ask
    round_.talks = 1
    first = build_prompt("game_talk", Material(items=[view(round_, board.score)]), PROFILE).user
    round_.talks = 2
    second = build_prompt("game_talk", Material(items=[view(round_, board.score)]), PROFILE).user
    round_.talks = 3
    third = build_prompt("game_talk", Material(items=[view(round_, board.score)]), PROFILE).user
    assert "'머리부터' 쪽" in first and "'꼬리부터' 쪽" in second and "투표를 권하고" in third
    round_.talks = 4
    fourth = build_prompt("game_talk", Material(items=[view(round_, board.score)]), PROFILE).user
    assert "망설이게 되는 순간" in fourth and "바삭한" not in fourth
    round_.phase = "closing"
    closing = build_prompt("game_talk", Material(items=[view(round_, board.score)]), PROFILE).user
    round_.phase = "closed"
    closed = build_prompt("game_talk", Material(items=[view(round_, board.score)]), PROFILE).user
    assert "마감한다고 알리고" in closing and "이미 마감됐다" in closed
    assert all("투표를 더 권하지 않는다" in text and "바삭한" not in text for text in (closing, closed))
    round_.phase = "open"
    assert all("바삭한" not in text for text in (first, second, third)), "DJ의 까닭은 공개 전에 넣지 않는다"
    board.result(round_.vote_id, {"머리부터": 3, "꼬리부터": 9})
    board.settle()
    reveal = build_prompt("game_reveal", Material(items=[view(round_, board.score)]), PROFILE).user
    assert "'꼬리부터' 쪽을 골랐다" in reveal and "DJ가 맞혔다" in reveal
    assert '<data label="투표 결과">\n머리부터: 3표\n꼬리부터: 9표\n</data>' in reveal
    assert "DJ 1점, 청취자 0점" in reveal
    late = dict(view(round_, board.score), outcome="late")
    text = build_prompt("game_reveal", Material(items=[late]), PROFILE).user
    assert "투표 결과" not in text.split("<data")[0] or "지어내지 않고" in text
    assert '<data label="투표 결과">' not in text and "점이라고 알린다" not in text
    final = build_prompt("game_final", Material(extra={"score": {"dj": 1, "listeners": 2, "draw": 0}}), PROFILE).user
    assert "DJ 1점, 청취자 2점으로 청취자가 이겼다" in final
    assert {"game_ask", "game_talk", "game_reveal", "game_final"} <= set(STEP_KINDS) and "game_round" not in STEP_KINDS


# ── 방송 통합 ─────────────────────────────────────────────────────────

class Backend:
    """제출을 받으면 바로 재생을 시작한 것으로 치고, 투표는 delay초 뒤에 결과를 돌려준다."""

    def __init__(self, *, delay: float | None = 0.05, counts=(7, 3)):
        self.engine: StationEngine | None = None
        self.segments, self.events, self.votes = [], [], {}
        self.delay, self.counts = delay, counts

    async def publish_segment(self, sub):
        self.segments.append(sub)
        self.engine.on_segment_started(sub.id, time.time())
        vote = self.votes.get(sub.id)
        if vote is not None and self.delay is not None:
            asyncio.get_running_loop().call_later(self.delay, self.close, vote)

    def close(self, vote):
        first, second = vote["options"]
        self.engine.on_vote_result({"vote_id": vote["vote_id"], "total": sum(self.counts),
                                    "counts": {first: self.counts[0], second: self.counts[1]}})

    async def notify_request_state(self, request_id, state):
        pass

    async def notify_station(self, event_type, payload):
        self.events.append((event_type, payload))
        if event_type == "vote.open":
            self.votes[payload["segment_id"]] = payload


def _game_engine(tmp_path, backend: Backend, *, emit_votes: bool = True, **settings) -> StationEngine:
    config = StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=30,
                           corners=["dj_vs_listeners"], topic="겨울 간식")
    engine = StationEngine(config, EngineSettings(
        audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "metrics.sqlite", safety_rules_path=RULES,
        target_buffer_sec=1e9, emit_votes=emit_votes, **settings), backend)
    backend.engine = engine
    return engine


def test_corner_runs_three_rounds_and_announces_the_score(tmp_path):
    backend = Backend()
    engine = _game_engine(tmp_path, backend)
    board = engine.sources.votes

    async def scenario():
        run = asyncio.create_task(engine.run())
        # 멘트 수로 끊지 않는다 — 생성 순서에 따라 다음 진입의 투표가 하나 더 끼기도 한다
        await _until(lambda: "코너 종료 · DJ 대 청취자" in _summaries(tmp_path), "코너를 끝까지 한다")
        engine.stop()
        await run

    asyncio.run(scenario())
    summaries = _summaries(tmp_path)
    corner = summaries[summaries.index("코너 진입 · DJ 대 청취자"):summaries.index("코너 종료 · DJ 대 청취자")]
    assert sum(s.startswith("투표 시작 · ") for s in corner) == 3, "코너 한 번에 세 라운드"
    results = [s for s in corner if s.startswith("투표 결과 · ")]
    assert len(results) == 3 and all(" 7 : " in s and ("DJ 적중" in s or "DJ 빗나감" in s) for s in results)
    assert "투표 고민 5개 준비" in summaries
    opens = [payload for event, payload in backend.events if event == "vote.open"][:3]
    first = opens[0]
    assert first["kind"] == "game" and first["window_sec"] == 60 and len(first["options"]) == 2
    submitted = {s.id for s in backend.segments}
    assert all(o["segment_id"] in submitted for o in opens) and len({o["vote_id"] for o in opens}) == 3
    assert board.rounds >= 3 and sum(board.score.values()) == board.rounds


def test_corner_falls_back_to_topic_talk_when_votes_are_off(tmp_path):
    backend = Backend()
    engine = _game_engine(tmp_path, backend, emit_votes=False)
    asyncio.run(engine.run(max_segments=6))
    assert not [event for event, _ in backend.events if event == "vote.open"]
    assert "dj_vs_listeners" not in {s.corner_type for s in backend.segments}
    assert "코너 대체 · DJ 대 청취자 (소재 없음)" in _summaries(tmp_path)


def test_late_result_is_given_up_after_the_window_and_ignored_afterwards(tmp_path):
    backend = Backend(delay=None)  # 결과를 보내지 않는다
    engine = _game_engine(tmp_path, backend, vote_window_sec=0.2)
    board = engine.sources.votes
    board.grace_sec = 0.1

    async def scenario():
        run = asyncio.create_task(engine.run())
        await _until(lambda: any(s.startswith("투표 결과 · ") for s in _summaries(tmp_path)), "첫 라운드를 접는다")
        first = next(payload for event, payload in backend.events if event == "vote.open")
        engine.on_vote_result({"vote_id": first["vote_id"], "counts": {first["options"][0]: 5, first["options"][1]: 1}})
        engine.stop()
        await run

    asyncio.run(scenario())
    results = [s for s in _summaries(tmp_path) if s.startswith("투표 결과 · ")]
    assert results[0] == "투표 결과 · 결과를 받지 못함"
    assert board.rounds == 0 and sum(board.score.values()) == 0, "늦게 온 결과는 점수에 넣지 않는다"


def test_corner_cut_drops_the_round_and_a_discarded_asking_segment_reopens_the_dilemma(tmp_path):
    backend = Backend(delay=None)
    engine = _game_engine(tmp_path, backend)
    board = engine.sources.votes

    async def scenario():
        run = asyncio.create_task(engine.run())
        await _until(lambda: board.active is not None and board.active.segment_id is not None, "투표를 연다")
        first = board.active
        await engine.on_segments_discarded({"segment_ids": [first.segment_id]})
        assert first.opened_at is not None, "이 백엔드는 제출 즉시 재생한다 — 열린 투표는 버려지지 않는다"
        engine.on_corner_skip({"corner": "topic_talk"})
        await _until(lambda: board.active is None, "코너가 끊기면 라운드를 접는다")
        left = board.count()
        engine.on_vote_result({"vote_id": first.vote_id, "counts": {"머리부터": 1, "꼬리부터": 2}})
        engine.stop()
        await run
        return left

    left = asyncio.run(scenario())
    assert left == 4, "방송에 나간 고민은 되돌리지 않는다"
    assert board.rounds == 0 and sum(board.score.values()) == 0


def test_manager_routes_vote_result_and_rejects_bad_payloads(tmp_path):
    backend = Backend(delay=None)
    engine = _game_engine(tmp_path, backend)
    board = engine.sources.votes
    board.add([BUNG])
    round_ = board.start()
    manager = EngineManager(EngineSettings())

    async def scenario():
        for bad in ({}, {"vote_id": round_.vote_id}, {"vote_id": round_.vote_id, "counts": {"머리부터": "3"}},
                    {"vote_id": 3, "counts": {}}, {"vote_id": round_.vote_id, "counts": {"머리부터": -1}}):
            with pytest.raises(ValueError):
                await manager._route_station_event(engine, {"type": "vote.result", "payload": bad})
        await manager._route_station_event(engine, {"type": "vote.result", "payload": {
            "vote_id": round_.vote_id, "counts": {"머리부터": 3, "꼬리부터": 9}, "total": 12}})

    asyncio.run(scenario())
    engine.telemetry.close()
    assert round_.counts == {"머리부터": 3, "꼬리부터": 9} and round_.outcome == MATCH
