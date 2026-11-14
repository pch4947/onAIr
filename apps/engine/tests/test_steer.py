"""화제 전환 STEER — 판별, 조건, 인용 뒤 화제 변경 (CORNER_DESIGN 5.2절)."""
import asyncio
import itertools
import sqlite3
from pathlib import Path

import pytest

from onair_engine.corners.run import CornerRun, MaterialSources, Thread, TrackSource
from onair_engine.corners.steps import STEER_RULE, build_prompt
from onair_engine.domain import ListenerRequest, Material, StationConfig, StationProfile
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.format import ClockEntry, load_format
from onair_engine.inputs.classify import steer_rule
from onair_engine.inputs.rules import load_chat_rules
from onair_engine.sources.rss import RssCollector
from test_scheduler import short_talk

ROOT = Path(__file__).resolve().parent.parent
RULES = load_chat_rules(ROOT / "config" / "chat_rules.yaml")
PROFILE = StationProfile(dj_name="새벽", tone="차분한 존댓말", concept="테스트 방송")
QUESTION = "요즘 듣는 재즈 앨범 있으세요?"


@pytest.mark.parametrize(("body", "rule"), [
    (QUESTION, "question"),
    ("여행 얘기 해 주세요", "request"),
    ("여행 얘기 해줘", "request"),
    ("dj님 오늘 목소리 좋아요", "address"),
    ("새벽님 오늘 목소리 좋아요", "address"),  # 방의 DJ 이름
    ("저는 오늘 시험 끝났어요", None),
    ("왜요?", None),  # 화제가 될 내용이 없다
    ("ㅋㅋㅋㅋㅋㅋ?", None),
])
def test_steer_rule(body, rule):
    assert steer_rule(body, RULES, "새벽") == rule


def _run(corner="topic_talk") -> CornerRun:
    fmt = load_format("talk", ROOT / "config" / "formats")
    sources = MaterialSources(tracks=TrackSource(), rss=RssCollector())
    sources.talk_points = lambda run: {"source": "topic"}  # 말할 거리가 떨어지지 않는다
    return CornerRun(ClockEntry(fmt.corner(corner)), sources=sources, intensity=fmt.intensity["mid"],
                     thread=Thread())


def test_can_steer_needs_count_budget_and_a_remaining_talk_step():
    run = _run()
    while run.next_step().step.kind != "talk_open":  # call → talk_open
        pass
    assert run.can_steer(1)
    assert not run.can_steer(0)  # 참여 강도 low
    run.steers = 1
    assert not run.can_steer(1)  # 상한
    run.steers = 0
    run.spent_ms = run.spec.budget_sec[1] * 1000 - 1000
    assert not run.can_steer(1)  # 한 단계 시간이 안 남았다
    run.spent_ms = 0
    for _ in range(5):  # common.yaml의 이야기 단계 반복 수
        assert run.next_step().step.kind == "talk"
    assert not run.can_steer(1)  # 마지막 이야기 — 바뀐 화제로 말할 단계가 없다


def test_prompt_announces_steer_only_when_marked():
    quote = ListenerRequest(request_id="r", kind="chat", body=QUESTION, requester_ref="u",
                            received_at=0.0)
    plain = build_prompt("talk", Material(quotes=[quote], topic="주제"), PROFILE)
    steer = build_prompt("talk", Material(quotes=[quote], topic="주제", extra={"steer": "question"}),
                         PROFILE)
    assert STEER_RULE not in plain.user
    assert STEER_RULE in steer.user


# ── 방송 통합 ─────────────────────────────────────────────────────────

class Capture:
    """토크 코너에 들어가면 채팅을 보낸다 — 오프닝의 일반 인용에 먼저 쓰이지 않게 한다."""

    def __init__(self):
        self.engine: StationEngine | None = None
        self.body = ""
        self.sent = False

    async def publish_segment(self, sub):
        if sub.corner_type == "topic_talk" and not self.sent:
            self.sent = True
            await self.engine.submit_request("story", self.body, "u1", request_id="req_1")

    async def notify_request_state(self, request_id, state):
        pass

    async def notify_station(self, event_type, payload):
        pass


def _broadcast(tmp_path, body, *, intensity="mid", drop_quote=False):
    """(코너 안 단계별 (단계, 화제, 인용, 전환 표시) 목록, steer_log 행)."""
    cfg = StationConfig(station_id="st_test", profile=PROFILE, broadcast_minutes=30,
                        corners=["topic_talk"], intensity=intensity, topic="오늘의 주제")
    settings = EngineSettings(audio_dir=tmp_path / "audio", sqlite_path=tmp_path / "m.sqlite",
                              safety_rules_path=ROOT / "config" / "safety_rules.yaml",
                              chat_rules_path=ROOT / "config" / "chat_rules.yaml",
                              formats_dir=ROOT / "config" / "formats", target_buffer_sec=1e9)
    transport = Capture()
    transport.body = body
    engine = short_talk(StationEngine(cfg, settings, transport))
    transport.engine = engine
    pipeline = engine.scheduler.pipeline
    original = pipeline.run
    jobs = []

    async def spy(job):
        if job.corner_type == "topic_talk":
            jobs.append((job.step, job.material.topic, [q.request_id for q in job.material.quotes],
                         bool(job.material.extra.get("steer"))))
        sub = await original(job)
        if drop_quote and sub is not None and job.material.quotes:
            sub.request_ref, sub.input_refs = None, []  # LLM이 인용을 빠뜨린 경우
        return sub

    pipeline.run = spy
    asyncio.run(engine.run(max_segments=9))
    db = sqlite3.connect(tmp_path / "m.sqlite")
    try:
        return jobs, db.execute("SELECT request_id, rule, old_topic, new_topic FROM steer_log").fetchall()
    finally:
        db.close()


def test_question_becomes_topic_of_the_very_next_step(tmp_path):
    jobs, steers = _broadcast(tmp_path, QUESTION)
    quoted_at = next(i for i, job in enumerate(jobs) if job[2] == ["req_1"])
    assert jobs[quoted_at][3] is True
    assert all(job[1] == "오늘의 주제" for job in jobs[:quoted_at + 1])
    # 화제 전환은 그 코너에 들어와 있는 동안만 이어진다 — 곡을 지나 다시 들어온 주제 토크(talk_resume)는
    # 원래 주제로 돌아간다. 채팅이 일찍 인용된 회차에는 그 멘트까지 만들어지므로 같은 진입의 멘트만 본다 (#280)
    same_entry = list(itertools.takewhile(lambda job: job[0] == "talk", jobs[quoted_at + 1:]))
    # 동시 생성이 2개여도 바로 다음 단계부터 새 화제다 — 인용 결과를 기다린다
    assert same_entry and [job[1] for job in same_entry] == [QUESTION] * len(same_entry)
    assert all(job[1] == "오늘의 주제" for job in jobs[quoted_at + 1 + len(same_entry):])
    assert steers == [("req_1", "question", "오늘의 주제", QUESTION)]


def test_plain_chat_is_quoted_without_changing_topic(tmp_path):
    jobs, steers = _broadcast(tmp_path, "저는 오늘 시험 끝났어요")
    assert any(job[2] == ["req_1"] for job in jobs)
    assert {job[1] for job in jobs} == {"오늘의 주제"}
    assert steers == []


def test_low_intensity_never_steers(tmp_path):
    jobs, steers = _broadcast(tmp_path, QUESTION, intensity="low")
    assert any(job[2] == ["req_1"] for job in jobs)
    assert {job[1] for job in jobs} == {"오늘의 주제"}
    assert steers == []


def test_missing_quote_does_not_change_topic_and_broadcast_continues(tmp_path):
    jobs, steers = _broadcast(tmp_path, QUESTION, drop_quote=True)
    quoted_at = next(i for i, job in enumerate(jobs) if job[2] == ["req_1"])
    assert len(jobs) > quoted_at + 1  # 대기 표시가 풀려 다음 단계가 나온다
    assert {job[1] for job in jobs} == {"오늘의 주제"}
    assert steers == []
