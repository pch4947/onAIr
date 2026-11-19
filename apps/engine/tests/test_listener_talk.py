"""청취자 글을 받는 자리 (2026-11-19 결정, ENGINE_TUNING_LOG T-016).

방송 도중의 다시 꺼내기는 끄고(test_talk_material), 질문은 곡 앞에서 던져 곡 뒤에 받고, 불만은 받지 않고
농담은 농담으로 받고, 모든 멘트에 시간대를 알린다.
"""
import asyncio
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from onair_engine.corners.run import CornerRun, PlannedStep, Thread
from onair_engine.corners.steps import (
    ANSWER_QUOTE_RULE,
    ASK_AFTER_QUOTE_RULE,
    ASK_NEW_RULE,
    ASK_RULE,
    ASKED_ALONE_RULE,
    ASKED_EARLIER_RULE,
    ASKED_REPLIES_RULE,
    BACK_ANNOUNCE_RULE,
    DAYTIME_RULE,
    JOKE_RULE,
    MULTI_QUOTE_RULE,
    PREV_TRACK,
    QUOTE_RULE,
    STEER_RULE,
    STEERED_RULE,
    build_prompt,
)
from onair_engine.domain import (
    TONE_COMPLAINT,
    TONE_JOKE,
    TONE_PLAIN,
    GenerationJob,
    ListenerRequest,
    Material,
    Prompt,
    RequestState,
    Script,
)
from onair_engine.format.clock import ClockEntry
from onair_engine.inputs.chat_tone import ChatToneJudge, parse_tone
from onair_engine.inputs.chat_tone import build_prompt as tone_prompt
from onair_engine.pipeline.llm import DummyLlmClient, LlmError, detect_step
from onair_engine.prompt_data import ASKED, ASKED_BEFORE, TOPIC
from onair_engine.scenario.listeners import Listener, Persona, asks_listeners
from onair_engine.scenario.listeners import build_prompt as listener_prompt
from onair_engine.scenario.runner import clock_offset_sec
from onair_engine.scheduler.scheduler import Asked, daytime, question_of
from test_quiet_flow import RecordingLlm
from test_scheduler import PROFILE, _engine

KST = timezone(timedelta(hours=9))
TRACK = {"track_ref": "yt:abc", "title": "새벽 버스", "artist": "모의 밴드", "duration_ms": 200_000}
QUESTION = "겨울 간식 하면 뭐가 먼저 떠오르세요?"


def _chat(body: str, rid: str = "c1", tone: str | None = None) -> ListenerRequest:
    return ListenerRequest(request_id=rid, kind="chat", body=body, requester_ref=rid, received_at=time.time(),
                           tone=tone)


def _run(scheduler, corner: str = "topic_talk") -> CornerRun:
    run = CornerRun(ClockEntry(scheduler.fmt.corner(corner)), sources=scheduler.sources,
                    intensity=scheduler.intensity, thread=Thread(topic="겨울 간식"))
    scheduler._run = run
    return run


# ── 시간대 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("at", "part", "clock", "others"), [
    (datetime(2026, 11, 19, 4, 10, tzinfo=KST), "새벽", "4시쯤", "아침이나 낮, 저녁"),
    (datetime(2026, 11, 19, 5, 40, tzinfo=KST), "새벽", "5시쯤", "아침이나 낮, 저녁"),  # 해 뜨기 전이다
    (datetime(2026, 11, 19, 7, 0, tzinfo=KST), "아침", "7시쯤", "낮이나 저녁, 밤"),
    (datetime(2026, 11, 19, 19, 30, tzinfo=KST), "저녁", "7시쯤", "아침이나 낮, 새벽"),
    (datetime(2026, 11, 20, 0, 20, tzinfo=KST), "밤", "12시쯤", "아침이나 낮"),
])
def test_daytime_names_the_part_of_day_and_what_does_not_fit(at, part, clock, others):
    assert daytime(at.timestamp()) == {"part": part, "clock": clock, "others": others}


@pytest.mark.parametrize("step", ["talk", "talk_resume", "music_intro", "closing_farewell", "filler"])
def test_every_ment_is_told_the_time_of_day(step):
    """화제를 꺼내는 단계에만 시각을 넣었더니 그 밖의 멘트가 새벽 4시에 "오늘 저녁"이라고 했다 (T-016)."""
    extra = {"daytime": {"part": "새벽", "clock": "4시쯤", "others": "아침이나 낮, 저녁"}}
    user = build_prompt(step, Material(topic="겨울 간식", track=dict(TRACK), extra=extra), PROFILE).user
    assert "방송 시간대: 새벽 4시쯤." in user and "아침이나 낮, 저녁처럼 말하지 않는다" in user
    assert "새벽이다" not in user, "DJ 이름이 '새벽'이면 '새벽다'로 엉킨다 — 시간대 낱말에 '이다'를 붙이지 않는다"
    assert "일부러 넣지 않는다" in user, "시간대를 말하라는 지시가 아니다 — 알려만 주면 멘트마다 시간대를 말한다"
    assert detect_step(user) == step
    assert DAYTIME_RULE.split("{")[0] not in build_prompt(step, Material(track=dict(TRACK)), PROFILE).user


def test_system_prompt_spells_out_the_dj_name():
    """ "DJ 새벽다."만으로는 이름을 '새벽다'로 읽을 때가 있다 — 시간대 낱말 '새벽'이 프롬프트에 함께 나오면 더 그렇다."""
    from onair_engine.domain import StationProfile

    profile = StationProfile(dj_name="새벽", tone="차분한 존댓말", concept="심야 스터디 라디오")
    system = build_prompt("opening_greeting", Material(), profile).system
    assert system.startswith("당신은 라디오 스테이션 [심야 스터디 라디오]의 DJ 새벽다. DJ의 이름은 '새벽'이다. 말투는")


def test_scheduler_puts_the_time_of_day_on_every_job(tmp_path):
    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    scheduler = engine.scheduler
    for corner in ("music_intro", "closing"):
        spec = scheduler.fmt.corner(corner)
        job = asyncio.run(scheduler._build_job(PlannedStep(spec, spec.steps[0], [dict(TRACK)], None)))
        assert set(job.material.extra["daytime"]) == {"part", "clock", "others"}


def test_scenario_sets_the_clock_the_dj_sees(tmp_path):
    """심야 방송을 낮에 시험한다 — 시나리오의 sim.clock이 DJ가 아는 시각을 정한다."""
    noon = datetime(2026, 11, 19, 13, 7, 30, tzinfo=KST).timestamp()
    offset = clock_offset_sec("04:10", noon)
    assert datetime.fromtimestamp(noon + offset, KST) == datetime(2026, 11, 19, 4, 10, tzinfo=KST)
    assert clock_offset_sec(None, noon) == 0.0
    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    scheduler = engine.scheduler
    scheduler.clock_offset_sec = clock_offset_sec("04:10", time.time())
    spec = scheduler.fmt.corner("music_intro")
    job = asyncio.run(scheduler._build_job(PlannedStep(spec, spec.steps[0], [dict(TRACK)], None)))
    assert job.material.extra["daytime"]["part"] == "새벽" and job.material.extra["daytime"]["clock"] == "4시쯤"


# ── 화제 전환 ──────────────────────────────────────────────────────────

def test_steer_does_not_promise_what_comes_next():
    """ "이 얘기를 조금 더 해 보겠다"는 오늘 주제를 예고하는 말로 나와 다음 멘트와 어긋났다 (T-016)."""
    quote = _chat("새벽님은 붕어빵 어디부터 드세요?")
    user = build_prompt("talk", Material(quotes=[quote], topic="겨울 간식", extra={"steer": "question"}), PROFILE).user
    assert STEER_RULE in user and "예고하는 말로 멘트를 끝내지" in user and "해 보겠다고" not in user
    steered = build_prompt("talk", Material(topic=quote.body, extra={"steered": True}), PROFILE).user
    assert STEERED_RULE in steered and "이미 한 답을 되풀이하지 않고" in steered


def test_steered_topic_gets_one_more_ment(tmp_path):
    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    scheduler = engine.scheduler
    run = _run(scheduler)
    quote = _chat("새벽님은 붕어빵 어디부터 드세요?")
    job = GenerationJob(job_id="j", corner_type="topic_talk", step="talk",
                        material=Material(quotes=[quote], extra={"steer": "question"}))
    scheduler._apply_steer(quote, job, "seg_1")
    assert scheduler.chat_rules.steer_points == 1 and run.thread.free_points == 1 and run.thread.steered


# ── 불만과 농담 ────────────────────────────────────────────────────────

class ToneLlm:
    def __init__(self, reply, delay: float = 0.0):
        self.reply, self.delay, self.prompts = reply, delay, []

    async def complete(self, prompt: Prompt, *, max_tokens: int) -> str:
        self.prompts.append(prompt)
        await asyncio.sleep(self.delay)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_parse_tone():
    assert parse_tone('판정: {"tone": "joke"}') == TONE_JOKE
    for bad in ("없음", '{"tone": "angry"}', '["joke"]'):
        with pytest.raises(ValueError):
            parse_tone(bad)


def test_tone_judge_asks_the_llm_and_falls_back_to_plain():
    chat = _chat("제 채팅은 또 씹으시네")
    llm = ToneLlm('{"tone": "complaint"}')
    assert asyncio.run(ChatToneJudge(llm, "겨울 간식").judge(chat)) == TONE_COMPLAINT
    user = llm.prompts[0].user
    assert f'<data label="{TOPIC}">\n겨울 간식\n</data>' in user and "제 채팅은 또 씹으시네" in user
    # 판정이 늦거나 실패하면 보통 채팅이다 — 판정 때문에 채팅이 방송에서 빠지면 안 된다
    slow = ChatToneJudge(ToneLlm('{"tone": "complaint"}', delay=0.2), "겨울 간식", timeout_sec=0.01)
    assert asyncio.run(slow.judge(chat)) == TONE_PLAIN
    for reply in ("모르겠음", LlmError("장애")):
        assert asyncio.run(ChatToneJudge(ToneLlm(reply), "겨울 간식").judge(chat)) == TONE_PLAIN
    dummy = asyncio.run(DummyLlmClient(delay_sec=0).complete(tone_prompt("겨울 간식", "ㅋㅋ"), max_tokens=100))
    assert parse_tone(dummy) == TONE_PLAIN


class FakeTone:
    def __init__(self, tones: dict[str, str], delay: float = 0.02):
        self.tones, self.delay = tones, delay

    async def judge(self, req: ListenerRequest) -> str:
        await asyncio.sleep(self.delay)
        return self.tones.get(req.body, TONE_PLAIN)


def test_operating_engine_judges_chat_tone(tmp_path):
    engine, _ = _engine(tmp_path, tone=True, corners=["topic_talk"])
    assert isinstance(engine.scheduler.tone_judge, ChatToneJudge)
    quiet, _ = _engine(tmp_path / "off", corners=["topic_talk"])
    assert quiet.scheduler.tone_judge is None


def test_complaints_leave_the_pool_and_jokes_stay_marked(tmp_path):
    engine, transport = _engine(tmp_path, corners=["topic_talk"])
    scheduler = engine.scheduler
    scheduler.tone_judge = FakeTone({"간식 얘기 그만하고 노래나 트세요": TONE_COMPLAINT, "핫팩 먹어도 돼요?": TONE_JOKE})
    chats = [_chat(body, f"c{i}") for i, body in enumerate(
        ["간식 얘기 그만하고 노래나 트세요", "핫팩 먹어도 돼요?", "저는 호빵파예요"])]

    async def scenario():
        for chat in chats:
            scheduler.enqueue_request(chat)
        assert scheduler.pool.quotable() == [] and len(scheduler.pool) == 3, "판정을 기다리는 동안은 후보가 아니다"
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert [c.tone for c in chats] == [TONE_COMPLAINT, TONE_JOKE, TONE_PLAIN]
    assert [c.body for c in scheduler.pool.quotable()] == ["핫팩 먹어도 돼요?", "저는 호빵파예요"]
    assert chats[0].state == RequestState.EXPIRED and ("c0", "expired") in [
        (rid, str(state)) for rid, state in transport.states], "불만은 받지 않는다 — 인용도 분위기 전달도 하지 않는다"


def test_joke_is_taken_lightly_and_does_not_become_the_topic(tmp_path):
    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    scheduler = engine.scheduler
    run = _run(scheduler)
    while run.next_step().step.kind != "talk_open":
        pass
    joke, question = _chat("핫팩 먹어도 돼요?", tone=TONE_JOKE), _chat("핫팩 먹어도 돼요?", "c2")
    marked, plain = Material(quotes=[joke]), Material(quotes=[question])
    scheduler._mark_steer(marked)
    scheduler._mark_steer(plain)
    assert "steer" not in marked.extra and plain.extra["steer"] == "question", "농담은 화제로 삼지 않는다"
    user = build_prompt("talk", Material(quotes=[joke], topic="겨울 간식"), PROFILE).user
    assert JOKE_RULE.format(which="이 채팅은") in user
    both = build_prompt("talk", Material(quotes=[_chat("저는 호빵파예요", "c3"), joke], topic="겨울 간식"), PROFILE).user
    assert JOKE_RULE.format(which="2번째 채팅은") in both
    assert "농담" not in build_prompt("talk", Material(quotes=[question], topic="겨울 간식"), PROFILE).user


def test_joke_is_not_kept_for_the_closing_lookback(tmp_path):
    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    scheduler = engine.scheduler
    job = GenerationJob(job_id="j", corner_type="topic_talk", step="talk", material=Material())
    scheduler._reflect(_chat("핫팩 먹어도 돼요", tone=TONE_JOKE), job, "seg_1", time.time())
    scheduler._reflect(_chat("저는 호빵파예요", "c2"), job, "seg_2", time.time())
    assert [a.body for a in scheduler.callbacks.lookback()] == ["저는 호빵파예요"]


# ── 곡 앞에서 묻고 곡 뒤에 받는다 ──────────────────────────────────────

def test_question_of_keeps_only_what_was_asked():
    script = f"오늘은 여기까지 이야기했네요. {QUESTION} 곡이 나가는 동안 채팅으로 남겨 주세요. 모의 밴드의 새벽 버스입니다."
    assert question_of(script) == QUESTION
    assert question_of("모의 밴드의 새벽 버스 듣겠습니다.") == ""
    # 물음표 없이 물은 말 (시험 실행 20261119-140625 — 이 질문은 곡 뒤에 돌아오지 못했다)
    calm = "호빵이 식었을 때 단맛이 더 또렷하게 느껴진 적 있으신가요. 듣는 동안 채팅으로 답을 남겨 주세요. 첫눈 오는 밤 듣겠습니다."
    assert question_of(calm) == "호빵이 식었을 때 단맛이 더 또렷하게 느껴진 적 있으신가요."


def test_song_intro_asks_before_the_song():
    asking = build_prompt("music_intro", Material(topic="겨울 간식", track=dict(TRACK), extra={"ask": True}), PROFILE).user
    assert ASK_RULE in asking and detect_step(asking) == "music_intro" and "물음표로 끝낸다" in asking
    assert f'<data label="{TOPIC}">\n겨울 간식\n</data>' in asking and "넓은 질문은 하지 않는다" in asking
    assert ASK_NEW_RULE not in asking and ASK_AFTER_QUOTE_RULE not in asking
    assert ASK_RULE not in build_prompt("music_intro", Material(topic="겨울 간식", track=dict(TRACK)), PROFILE).user
    again = build_prompt("music_intro", Material(
        track=dict(TRACK), quotes=[_chat("저는 호빵파예요")],
        extra={"ask": True, "asked_before": [QUESTION]}), PROFILE).user
    assert ASK_NEW_RULE in again and f'<data label="{ASKED_BEFORE}">\n- {QUESTION}\n</data>' in again
    assert ASK_AFTER_QUOTE_RULE in again, "채팅을 받는 곡 소개에서는 받고 나서 묻는다"


def _asking(tmp_path, **config):
    engine, _ = _engine(tmp_path, dummy_tracks=False, corners=config.pop("corners", ["topic_talk"]), **config)
    scheduler = engine.scheduler
    scheduler._corner_before = "topic_talk"  # 이야기 코너 바로 뒤의 곡이다
    return scheduler, Material(topic="겨울 간식", track=dict(TRACK))


def test_asks_only_when_the_answer_can_be_taken(tmp_path):
    scheduler, material = _asking(tmp_path)
    now = time.time()
    assert scheduler._should_ask(material, now)
    for track in ({"dummy": True}, {"requester_nickname": "밤샘러"}):  # 실제로 나가지 않는 곡, 신청곡 소개
        assert not scheduler._should_ask(Material(track={**TRACK, **track}), now)
    scheduler._corner_before = "opening"
    assert not scheduler._should_ask(material, now), "이야기 코너 뒤의 곡 앞에서만 묻는다"
    scheduler._corner_before = "topic_talk"
    scheduler._asked = Asked("seg_0", QUESTION, now)
    assert not scheduler._should_ask(material, now), "던진 질문을 받기 전에는 또 묻지 않는다"
    scheduler._asked = None
    scheduler._started_at, scheduler._end_at = now, now + 300
    assert not scheduler._should_ask(material, now), "곡 뒤에 답을 받을 시간이 없다"
    scheduler._started_at = scheduler._end_at = None
    scheduler.intensity = scheduler.fmt.intensity["low"]
    assert not scheduler._should_ask(material, now), "참여 강도 low는 묻지 않는다"


def test_asks_less_often_when_nobody_is_chatting(tmp_path):
    scheduler, material = _asking(tmp_path)
    now = time.time()
    scheduler._last_question_at = now - scheduler.chat_rules.question_active_gap_sec - 1
    assert not scheduler._should_ask(material, now), "조용하면 question_gap_sec에 한 번"
    scheduler.enqueue_request(_chat("저는 호빵파예요"))
    assert scheduler._should_ask(material, now), "채팅이 오가면 question_active_gap_sec에 한 번"
    scheduler._last_question_at = now
    assert not scheduler._should_ask(material, now)


def test_talk_corner_comes_back_first_when_another_corner_is_next(tmp_path):
    """곡이 나가는 동안 문자나 사연이 오면 곡 뒤 차례가 그 코너가 된다 — 이야기 코너가 먼저 들어와 답을 받는다."""
    scheduler, _ = _asking(tmp_path, corners=["story_corner"])
    assert scheduler._answer_corner() is None, "던진 질문이 없다"
    scheduler._asked = Asked("seg_q", QUESTION, time.time(), None, "topic_talk")
    assert scheduler._answer_corner() is None, "사연이 없으면 사연 코너는 주제 토크로 대체된다 — 그대로 둔다"
    scheduler.sources.satisfies = lambda corner, label=None: True  # 사연이 와 있다 — 사연 코너는 답을 받지 않는다
    entry = scheduler._answer_corner()
    assert entry is not None and entry.corner.corner_type == "topic_talk"
    upcoming = [e.corner.corner_type for e in scheduler.clock.upcoming(2)]
    planned = scheduler._next_planned(scheduler._snapshot())
    assert planned.corner.corner_type == "topic_talk"
    assert [e.corner.corner_type for e in scheduler.clock.upcoming(2)] == upcoming, "클락의 순환은 그대로다"
    scheduler._asked = Asked("seg_q", QUESTION, time.time() - scheduler.chat_rules.question_open_sec - 1, None,
                             "topic_talk")
    assert scheduler._answer_corner() is None, "오래된 질문 때문에 코너를 바꾸지 않는다"


def test_ment_after_the_song_returns_to_the_question():
    asked = {"asked": QUESTION, "after_song": dict(TRACK), "back_announce": True}
    alone = build_prompt("talk_resume", Material(topic="겨울 간식", extra=dict(asked)), PROFILE).user
    assert f'<data label="{ASKED}">\n{QUESTION}\n</data>' in alone and ASKED_ALONE_RULE in alone
    assert BACK_ANNOUNCE_RULE not in alone and PREV_TRACK not in alone, "질문으로 돌아오는 멘트는 곡까지 받지 않는다"
    assert detect_step(alone) == "talk_resume"
    replied = build_prompt("talk_resume", Material(topic="겨울 간식", quotes=[_chat("저는 호빵이요")],
                                                   extra=dict(asked)), PROFILE).user
    assert ASKED_REPLIES_RULE in replied and ASKED_ALONE_RULE not in replied
    assert "답이 없다는 말은 하지 않고" in ASKED_REPLIES_RULE, "채팅은 왔는데 답인 글이 없을 수 있다"
    assert replied.index(ASKED) < replied.index("저는 호빵이요"), "질문을 먼저 알려 주고 채팅을 보여 준다"
    assert "이 멘트는 곡 전에 무엇을 물었는지 한마디로 되짚는 문장으로 시작한다" in replied
    assert QUOTE_RULE in replied and ANSWER_QUOTE_RULE not in replied
    # 질문 뒤에 온 글을 후보로 주면 답인 것만 골라 받게 한다 — 다 받으라고 하지 않는다
    picking = build_prompt("talk_resume", Material(
        topic="겨울 간식", quotes=[_chat("저는 호빵이요"), _chat("내일 출근하기 싫어요", "c2")],
        extra={**asked, "optional_quotes": True}), PROFILE).user
    assert ANSWER_QUOTE_RULE in picking and QUOTE_RULE not in picking and MULTI_QUOTE_RULE not in picking
    block = build_prompt("talk_block", Material(topic="겨울 간식", extra={"asked": QUESTION, "parts": 4}), PROFILE).user
    assert "첫 문단은 곡 전에 무엇을 물었는지 한마디로 되짚는 문장으로 시작한다" in block and ASKED_ALONE_RULE in block
    # 답은 곡 뒤 첫 멘트가 다 받지 못한다 — 뒤 멘트가 받는 답도 질문과 함께 받는다
    later = build_prompt("talk", Material(topic="겨울 간식", quotes=[_chat("군고구마요")],
                                          extra={"asked_earlier": QUESTION}), PROFILE).user
    assert ASKED_EARLIER_RULE in later and QUESTION in later
    assert ASKED not in build_prompt("talk", Material(topic="겨울 간식", extra={"asked_earlier": QUESTION}), PROFILE).user


def test_ment_after_the_song_waits_until_the_song_is_almost_over(tmp_path):
    """백엔드가 알리는 남은 분량에는 곡이 없다 — 두면 곡 소개가 끝나자마자 곡 뒤 멘트를 만들어 답을 받지 못한다."""
    scheduler, _ = _asking(tmp_path)
    scheduler.target_buffer_sec = 30
    now = time.time()
    assert not scheduler._waits_for_answers(now)
    anchor = scheduler.timeline.new_anchor("track:1", "track", [])
    scheduler._asked = Asked("seg_q", QUESTION, now, anchor.id)
    assert scheduler._waits_for_answers(now), "곡 소개가 아직 제출되지 않았다 — 잠깐 기다린다"
    assert not scheduler._waits_for_answers(now + 31), "오래 기다리지는 않는다"
    scheduler.timeline.append("seg_q", anchor, 10_000, now, track_ref="yt:abc", track_ms=120_000)
    assert scheduler._waits_for_answers(now + 60), "곡이 한창이다"
    assert scheduler._waits_for_answers(now + 99) and not scheduler._waits_for_answers(now + 101), \
        "곡이 끝나기 30초(target_buffer_sec) 전부터 만든다"
    # 백엔드가 알리는 시작 시각은 청취자가 듣는 시각이다 — 방송 지연 12초만큼 일찍 만들어야 곡 뒤가 비지 않는다
    scheduler.on_track_started("yt:abc", time.time() + 12)
    end = scheduler.timeline.track_end(anchor.id)
    assert scheduler._air_delay_sec == pytest.approx(12, abs=0.5)
    assert scheduler._waits_for_answers(end - 43) and not scheduler._waits_for_answers(end - 41)


@pytest.mark.parametrize(("asked", "contract", "taken"), [
    (True, 2, 6),  # 질문 뒤에 온 글을 후보로 넉넉히 준다 — 답인 것만 대본이 고른다
    (False, 2, 1),  # 평소에는 참여 강도대로
    (True, 1, 1),  # 계약 v1은 세그먼트에 입력 하나만 싣는다
])
def test_answer_ment_gets_what_came_after_the_question_as_candidates(tmp_path, asked, contract, taken):
    scheduler, _ = _asking(tmp_path)
    scheduler._contract_version = contract
    run = _run(scheduler)
    bodies = ["저는 호빵이요", "군고구마 하나요", "붕어빵 팥으로요", "어묵 국물이 최고죠", "전 귤이요", "호떡이죠",
              "내일 출근하기 싫어요"]
    for i, body in enumerate(bodies):
        scheduler.enqueue_request(_chat(body, f"c{i}"))
    anchor = scheduler.timeline.new_anchor("topic:1", "topic", ["겨울", "간식"])
    extra = {"asked": QUESTION, "asked_at": time.time() - 100} if asked else {}
    material = Material(topic="겨울 간식", extra=extra)
    assert len(scheduler._take_quotes(run.spec.mode("chat"), "talk_resume", anchor, material)) == taken
    assert bool(material.extra.get("optional_quotes")) is (taken > 1)
    scheduler._mark_steer(material)


def test_answer_from_the_listener_just_quoted_is_not_skipped(tmp_path):
    """연달아 받지 않는 규칙에 걸려 하나뿐인 답을 놓치고 DJ가 혼자 답했다 (본 실행 20261119-143036)."""
    scheduler, _ = _asking(tmp_path)
    scheduler._contract_version = 2
    run = _run(scheduler)
    scheduler._last_requester = "same"
    answer, other = _chat("전 무조건 팥이요", "a1"), _chat("새벽님은 고구마 먹을 때 뭐랑 드세요?", "q1")
    answer.requester_ref = "same"
    for chat in (answer, other):
        scheduler.enqueue_request(chat)
    anchor = scheduler.timeline.new_anchor("topic:1", "topic", ["겨울", "간식"])
    material = Material(topic="겨울 간식", extra={"asked": QUESTION, "asked_at": time.time() - 100})
    picked = scheduler._take_quotes(run.spec.mode("chat"), "talk_resume", anchor, material)
    assert {req.request_id for req in picked} == {"a1", "q1"}


def test_answer_ment_prefers_what_came_after_the_question(tmp_path):
    scheduler, _ = _asking(tmp_path)
    scheduler._contract_version = 2
    run = _run(scheduler)
    old = [_chat("새벽님은 물고구마파예요 밤고구마파예요?", "old1"), _chat("겨울엔 역시 붕어빵이죠", "old2")]
    for chat in old:
        chat.received_at -= 200
        scheduler.enqueue_request(chat)
    asked_at = time.time() - 100
    scheduler.enqueue_request(_chat("전 무조건 마셔요", "reply"))
    anchor = scheduler.timeline.new_anchor("topic:1", "topic", ["겨울", "간식"])
    material = Material(topic="겨울 간식", extra={"asked": QUESTION, "asked_at": asked_at})
    picked = scheduler._take_quotes(run.spec.mode("chat"), "talk_resume", anchor, material)
    assert [req.request_id for req in picked] == ["reply"], "질문 뒤에 온 글이 있으면 그 가운데서 고른다"
    assert {req.request_id for req in scheduler.pool.quotable()} == {"old1", "old2"}, "먼저 와 있던 글은 뒤 멘트가 받는다"
    scheduler._last_quote_at = float("-inf")
    none_after = Material(topic="겨울 간식", extra={"asked": QUESTION, "asked_at": time.time() + 1})
    assert len(scheduler._take_quotes(run.spec.mode("chat"), "talk_resume", anchor, none_after)) == 1, \
        "질문 뒤에 온 글이 없으면 기다리던 글을 평소처럼 받는다"
    assert "optional_quotes" not in none_after.extra


def test_song_intro_is_told_what_was_already_asked(tmp_path):
    scheduler, _ = _asking(tmp_path)
    scheduler._questions = [f"질문 {i}?" for i in range(7)]
    spec = scheduler.fmt.corner("music_intro")
    job = asyncio.run(scheduler._build_job(PlannedStep(spec, spec.steps[0], [dict(TRACK)], None)))
    assert job.material.extra["ask"] is True and scheduler._ask_pending
    assert job.material.extra["asked_before"] == [f"질문 {i}?" for i in range(2, 7)]


def test_answers_are_counted_from_when_the_question_was_heard(tmp_path):
    """질문은 만든 뒤 20초쯤 지나서 나간다 — 그 사이에 온 글은 답이 아니다."""
    scheduler, _ = _asking(tmp_path)
    made = time.time() - 150
    anchor = scheduler.timeline.new_anchor("track:1", "track", [])
    scheduler.timeline.append("seg_q", anchor, 10_000, made, track_ref="yt:abc", track_ms=120_000)
    scheduler._asked = Asked("seg_q", QUESTION, made, anchor.id, "topic_talk")
    early = Material()
    scheduler._attach_asked(early)
    assert early.extra["asked_at"] == made, "재생 시작을 아직 모르면 만든 때부터 센다"
    scheduler.timeline.sync_started("seg_q", made + 32)  # 청취자에게 들리기 시작한 시각
    scheduler._asked = Asked("seg_q", QUESTION, made, anchor.id, "topic_talk")
    heard = Material()
    scheduler._attach_asked(heard)
    assert heard.extra["asked_at"] == made + 32


def test_old_or_unaired_question_is_dropped(tmp_path):
    scheduler, _ = _asking(tmp_path)
    scheduler._asked = Asked("seg_q", QUESTION, time.time() - scheduler.chat_rules.question_open_sec - 1)
    material = Material()
    scheduler._attach_asked(material)
    assert "asked" not in material.extra and scheduler._asked is None, "받을 때를 놓친 질문은 접는다"

    class Sub:
        id = "seg_q"

    scheduler._asked = Asked("seg_q", QUESTION, time.time())
    asyncio.run(scheduler._take_back(Sub(), None))
    assert scheduler._asked is None, "질문을 던진 멘트가 방송에 나가지 않았다"


def _question_broadcast(tmp_path, reply: str | None):
    engine, _ = _engine(tmp_path, dummy_tracks=False, corners=["topic_talk"], topic="겨울 간식")
    scheduler = engine.scheduler
    scheduler.pipeline.llm = llm = RecordingLlm()
    engine.topic_plan._llm.delay_sec = 0
    engine.on_track_queued({"track": dict(TRACK), "source": "playlist"})

    async def listener():
        while scheduler._asked is None:
            await asyncio.sleep(0.005)
        if reply:
            await engine.submit_request("chat", reply, "u1", request_id="in_reply")

    async def scenario():
        task = asyncio.create_task(listener())
        await engine.run(max_segments=14)
        task.cancel()

    asyncio.run(scenario())
    engine.telemetry.close()
    return [p.user for p in llm.generated]


def test_broadcast_asks_before_the_song_and_takes_the_answer_after_it(tmp_path):
    prompts = _question_broadcast(tmp_path, "저는 호빵이요")
    steps = [detect_step(user) for user in prompts]
    intro = steps.index("music_intro")
    assert ASK_RULE in prompts[intro]
    assert not [user for user in prompts[:intro] if "질문을 하나 던진다" in user], "곡 앞이 아니면 묻지 않는다"
    after = prompts[intro + 1]
    assert steps[intro + 1] == "talk_resume" and ASKED_REPLIES_RULE in after and "저는 호빵이요" in after
    assert f'<data label="{ASKED}">\n여러분은 어느 쪽이 더 좋으세요?\n</data>' in after
    assert sum(ASKED_REPLIES_RULE in user or ASKED_ALONE_RULE in user for user in prompts) == 1, "질문은 한 번 받는다"
    assert not [user for user in prompts if ASK_NEW_RULE in user], "처음 묻는 질문이다"
    db = sqlite3.connect(tmp_path / "metrics.sqlite")
    events = [e for (e,) in db.execute(
        "SELECT json_extract(data_json, '$.event') FROM event_log WHERE kind = 'question' ORDER BY at")]
    assert events == ["asked", "answered"]


class PickyLlm(RecordingLlm):
    """답을 받는 멘트에서 후보 가운데 하나만 받는다 — 실제 LLM이 답인 글만 고르는 것을 흉내 낸다."""

    async def generate(self, prompt):
        if ANSWER_QUOTE_RULE in prompt.user:
            self.generated.append(prompt)
            return Script(text="어느 쪽이 좋은지 여쭤봤죠. 저는 호빵이요라고 하신 분, 든든한 선택이네요.")
        return await super().generate(prompt)


def test_candidates_that_are_not_answers_go_back_without_a_failure(tmp_path):
    engine, _ = _engine(tmp_path, dummy_tracks=False, corners=["topic_talk"], topic="겨울 간식")
    scheduler = engine.scheduler
    scheduler._contract_version = 2
    scheduler.pipeline.llm = llm = PickyLlm()
    engine.topic_plan._llm.delay_sec = 0
    engine.on_track_queued({"track": dict(TRACK), "source": "playlist"})

    async def listener():
        while scheduler._asked is None:
            await asyncio.sleep(0.005)
        await engine.submit_request("chat", "저는 호빵이요", "u1", request_id="in_answer")
        await engine.submit_request("chat", "내일 출근하기 싫어요", "u2", request_id="in_other")

    async def scenario():
        task = asyncio.create_task(listener())
        await engine.run(max_segments=14)
        task.cancel()

    asyncio.run(scenario())
    engine.telemetry.close()
    answer = next(p.user for p in llm.generated if ANSWER_QUOTE_RULE in p.user)
    assert "저는 호빵이요" in answer and "내일 출근하기 싫어요" in answer
    assert scheduler._failures.get("in_other", 0) == 0, "답이 아니라서 받지 않은 글은 실패가 아니다"
    db = sqlite3.connect(tmp_path / "metrics.sqlite")
    results = dict(db.execute("SELECT request_ref, result FROM quote_log WHERE kind = 'candidate'"))
    assert results == {"in_answer": "ok", "in_other": "unpicked"}
    later = [p.user for p in llm.generated if "내일 출근하기 싫어요" in p.user and ANSWER_QUOTE_RULE not in p.user]
    assert later and QUOTE_RULE in later[0], "받지 않은 글은 다음 멘트가 평소처럼 받는다"


def test_dj_answers_the_question_when_nobody_does(tmp_path):
    prompts = _question_broadcast(tmp_path, None)
    after = prompts[[detect_step(user) for user in prompts].index("music_intro") + 1]
    assert ASKED_ALONE_RULE in after and ASKED_REPLIES_RULE not in after


# ── 가상 청취자 — DJ가 물으면 답한다 ───────────────────────────────────

def test_virtual_listeners_answer_when_the_dj_asks():
    """시험 실행에서 가상 청취자는 DJ의 질문에 거의 답하지 않았다 — 묻는 말을 들으면 평소보다 잘 반응하게 한다."""
    import random

    assert asks_listeners("speech", QUESTION) and not asks_listeners("song", "첫눈 오는 밤?")
    assert not asks_listeners("speech", "팥붕파예요, 슈붕파예요라고 물어보신 분, 저는 팥붕 쪽이에요.")
    persona = Persona(name="자기 얘기형", about="자기 경험을 쓴다.", speech_rate=0.14)

    def reactions(text: str) -> int:
        listener = Listener(id="a1", key="own_story", persona=persona, nickname="밤샘러", rng=random.Random(7))
        return sum(listener.wants_to_react("speech", text) for _ in range(2000))

    usual, asked = reactions("겨울 간식 이야기를 해 볼게요."), reactions(QUESTION)
    assert 200 < usual < 360 and 600 < asked < 800, "묻는 말에는 2.5배쯤 더 반응한다"
    sure = Persona(name="활발한 소통형", about="맞장구를 친다.", speech_rate=0.3, answer_rate=1.0)
    eager = Listener(id="b1", key="active", persona=sure, nickname="새벽반", rng=random.Random(7))
    assert all(eager.wants_to_react("speech", QUESTION) for _ in range(20))
    from onair_engine.scenario.listeners import Heard
    eager.heard.append(Heard("speech", QUESTION))
    assert "방금 DJ가 청취자들에게 물었다" in listener_prompt(eager, {"DJ 이름": "새벽"}, "chat").user
    eager.heard.append(Heard("speech", "모의 밴드의 새벽 버스 듣겠습니다."))
    assert "방금 DJ가 청취자들에게 물었다" not in listener_prompt(eager, {"DJ 이름": "새벽"}, "chat").user
