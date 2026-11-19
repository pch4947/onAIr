"""때를 놓친 반응 (2026-11-19 결정, ENGINE_TUNING_LOG T-017).

방금 들은 말에 기대는 글("완전 공감이에요")은 곡을 넘기면 한 건씩 받지 않는다. 반응을 뭉쳐서 전하는 자리는
곡 뒤 첫 멘트 하나이고, 그 멘트가 질문의 답을 받는 차례면 답만 받는다. 그 자리를 놓친 반응은 받지 않는다.
"""
import asyncio
import time

import pytest

from onair_engine.corners.run import PlannedStep
from onair_engine.corners.steps import (
    ASKED_ALONE_RULE,
    MOOD_HEARD_RULE,
    MOOD_RULE,
    QUOTE_RULE,
    build_prompt,
)
from onair_engine.domain import (
    ABOUT_SELF,
    ABOUT_SONG,
    ABOUT_TALK,
    TONE_PENDING,
    TONE_PLAIN,
    ChatType,
    GenerationJob,
    ListenerRequest,
    Material,
    RequestState,
)
from onair_engine.inputs.chat_tone import ChatToneJudge, parse_judgement
from onair_engine.inputs.chat_tone import build_prompt as tone_prompt
from onair_engine.inputs.pool import QuotePool, is_mood, leans
from onair_engine.pipeline.llm import DummyLlmClient
from onair_engine.pipeline.pipeline import mentioned, quoted
from onair_engine.prompt_data import HEARD
from test_listener_talk import QUESTION, TRACK, ToneLlm, _asking, _run
from test_scheduler import PROFILE, _engine

AGREE = "크 말 너무 좋다 서두르고 싶지 않다는 거 완전 공감이에요 ㅠㅠ"  # 방금 들은 말에 기대는 글
STORY = "독서실 끝나고 집 올 때 트럭에서 파는 땅콩빵 한 봉지 사서 하나씩 꺼내 먹어요"  # 혼자 뜻이 통하는 글
SONG = "노래 너무 포근하네요 ㅎㅎ 겨울밤이랑 찰떡이에요"


def _chat(body: str, rid: str = "c1", *, about: str | None = None, at: float | None = None,
          chat_type: ChatType = ChatType.STANDALONE, rule: str = "default", anchor_id: str | None = None,
          anchor_seq: int | None = None) -> ListenerRequest:
    if about is not None:
        chat_type = ChatType.STANDALONE if about == ABOUT_SELF else ChatType.REACTION
    return ListenerRequest(request_id=rid, kind="chat", body=body, requester_ref=rid,
                           received_at=time.time() if at is None else at, chat_type=chat_type, rule=rule,
                           about=about, anchor_id=anchor_id, anchor_seq=anchor_seq)


def _pool(track_end=lambda _: None) -> QuotePool:
    return QuotePool(reaction_window_sec=60, standalone_window_sec=600, mood_window_sec=420, track_end=track_end)


# ── 혼자 뜻이 통하는 글인가 ────────────────────────────────────────────

def test_tone_prompt_also_asks_whether_the_chat_stands_alone():
    heard = "곡: 첫눈 오는 밤 - 가상의 가수\n곡 전에 한 말: 오늘 밤의 간식도 서두르지 않고 천천히 가요."
    user = tone_prompt("겨울 간식", AGREE, heard).user
    for about in (ABOUT_SELF, ABOUT_TALK, ABOUT_SONG):
        assert f"- {about}:" in user
    assert HEARD in user and "서두르지 않고" in user, "무엇을 듣고 쓴 글인지 알아야 기대는 글인지 가린다"
    assert HEARD not in tone_prompt("겨울 간식", AGREE).user


def test_parse_judgement_reads_both_and_tolerates_a_missing_about():
    assert parse_judgement('{"tone": "plain", "about": "talk"}') == (TONE_PLAIN, ABOUT_TALK)
    assert parse_judgement('{"tone": "joke"}') == ("joke", None)
    assert parse_judgement('{"tone": "plain", "about": "모름"}') == (TONE_PLAIN, None), "모르는 값이면 규칙 분류를 따른다"
    with pytest.raises(ValueError):
        parse_judgement('{"about": "talk"}')


def test_judge_writes_what_the_chat_leans_on():
    chat = _chat(AGREE)
    chat.heard = "오늘 밤의 간식도 서두르지 않고 천천히 가요."
    llm = ToneLlm('{"tone": "plain", "about": "talk"}')
    assert asyncio.run(ChatToneJudge(llm, "겨울 간식").judge(chat)) == TONE_PLAIN
    assert chat.about == ABOUT_TALK and "서두르지 않고" in llm.prompts[0].user
    slow = _chat(AGREE)
    asyncio.run(ChatToneJudge(ToneLlm('{"tone": "plain", "about": "talk"}', delay=0.2), "겨울 간식",
                              timeout_sec=0.01).judge(slow))
    assert slow.about is None, "판정하지 못하면 규칙 분류를 따른다"
    dummy = asyncio.run(DummyLlmClient(delay_sec=0).complete(tone_prompt("겨울 간식", AGREE), max_tokens=100))
    assert parse_judgement(dummy) == (TONE_PLAIN, None)


def test_llm_judgement_overrides_the_rule_based_type(tmp_path):
    """규칙은 20자가 넘는 맞장구를 독립형으로, '방금'이 든 자기 이야기를 반응형으로 본다."""
    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    scheduler = engine.scheduler
    own = "공부하다가 방금 에어프라이어에 돌린 호빵 꺼내왔어요 호호 부는 중"

    class About:
        async def judge(self, req):
            req.about = ABOUT_TALK if req.body == AGREE else ABOUT_SELF
            return TONE_PLAIN

    scheduler.tone_judge = About()

    async def scenario():
        chats = [_chat(AGREE, "c1"), _chat(own, "c2")]
        for chat in chats:
            scheduler.enqueue_request(chat)
            assert chat.tone == TONE_PENDING
        await asyncio.gather(*scheduler._notices)
        return chats

    agree, story = asyncio.run(scenario())
    assert (agree.rule, agree.chat_type) == ("default", ChatType.REACTION)
    assert (story.rule, story.chat_type) == ("pointing", ChatType.STANDALONE)
    assert leans(agree) and not leans(story) and not is_mood(story)


# ── 후보 풀 ────────────────────────────────────────────────────────────

def test_leaning_chat_is_quoted_alone_only_while_its_talk_is_near():
    pool = _pool()
    agree = _chat(AGREE, about=ABOUT_TALK, at=100.0, anchor_id="topic:1", anchor_seq=1)
    story = _chat(STORY, "c2", about=ABOUT_SELF, at=100.0, anchor_id="topic:1", anchor_seq=1)
    pool.add(agree)
    pool.add(story)
    assert pool.expire(150.0, 1) == [] and pool.quotable() == [agree, story], "같은 이야기 안에서는 한 건씩 받는다"
    assert pool.expire(150.0, 3) == [], "곡을 넘겼다 — 버리지 않고 곡 뒤 첫 멘트가 분위기로 전하게 둔다"
    assert agree.stale and pool.quotable() == [story] and pool.reactions() == [agree]
    assert pool.expire(100.0 + 421, 3) == [agree], "곡 뒤 첫 멘트를 만나지 못하면 창이 지나 만료된다"
    assert pool.expire(100.0 + 601, 3) == [story]


def test_agreement_that_arrives_over_a_song_is_never_quoted_alone():
    """곡 소개나 곡이 나가는 동안 온 맞장구는 곡 전의 말에 대한 것이다."""
    pool = _pool(track_end=lambda anchor: 300.0)
    agree = _chat(AGREE, about=ABOUT_TALK, at=120.0, anchor_id="track:2", anchor_seq=2)
    reply = _chat("전 꼬리부터 먹는데 새벽님은 머리부터 드시나요?", "c2", about=ABOUT_TALK, at=120.0,
                  anchor_id="track:2", anchor_seq=2)
    song = _chat(SONG, "c3", about=ABOUT_SONG, at=120.0, anchor_id="track:2", anchor_seq=2)
    for chat in (agree, reply, song):
        pool.add(chat)
    assert pool.expire(130.0, 2) == []
    assert pool.quotable() == [reply], "되묻거나 묻는 글은 곡이 끝난 뒤 잠깐은 한 건씩 받는다"
    assert pool.reactions() == [agree, song]
    assert pool.answerable(100.0) == [agree, reply], "답 후보에는 맞장구도 든다 — 곡 감상은 뺀다"
    assert pool.expire(300.0 + 61, 3) == [reply], "되묻는 글은 그 자리를 지나면 받지 않는다"
    assert pool.drop_moods() == [agree, song] and len(pool) == 0


def test_chats_without_a_judgement_expire_as_before():
    pool = _pool()
    reaction = _chat("아이유 목소리 진짜 맑아요", chat_type=ChatType.REACTION, rule="anchor_keyword", at=100.0)
    short = _chat("대박 ㅋㅋ", "c2", chat_type=ChatType.REACTION, rule="short_reaction", at=100.0)
    pool.add(reaction)
    pool.add(short)
    assert pool.expire(161.0, None) == [reaction] and not reaction.stale
    assert pool.reactions() == [short]


# ── 뭉쳐서 전하는 자리 ─────────────────────────────────────────────────

def _opener(scheduler, *, after_song: bool, asked: bool = False):
    run = _run(scheduler)
    run.after_song = after_song
    scheduler._last_track = dict(TRACK)
    if asked:
        anchor = scheduler.timeline.new_anchor("track:9", "track", [])
        from onair_engine.scheduler.scheduler import Asked
        scheduler._asked = Asked("seg_q", QUESTION, time.time() - 5, anchor.id, "topic_talk")
    spec = run.spec
    step = next(s for s in spec.steps if s.kind in ("talk_open", "talk_resume"))
    return asyncio.run(scheduler._build_job(PlannedStep(spec, step, [], None)))


def _with_reactions(tmp_path):
    scheduler, _ = _asking(tmp_path)
    scheduler._contract_version = 2
    states: list[tuple[str, RequestState]] = []
    scheduler.telemetry.log_request_state = lambda rid, state: states.append((rid, state))
    for chat in (_chat(AGREE, "agree", about=ABOUT_TALK, anchor_id="track:1", anchor_seq=0),
                 _chat(SONG, "song", about=ABOUT_SONG, anchor_id="track:1", anchor_seq=0),
                 _chat(STORY, "story", about=ABOUT_SELF)):
        scheduler.pool.add(chat)
    return scheduler, states


def test_first_ment_after_the_song_carries_the_reactions_as_mood(tmp_path):
    scheduler, _ = _with_reactions(tmp_path)
    job = _opener(scheduler, after_song=True)
    assert [r.request_id for r in job.material.mood] == ["agree", "song"]
    assert [q.request_id for q in job.material.quotes] == ["story"], "혼자 뜻이 통하는 글은 평소처럼 한 건씩 받는다"
    user = build_prompt(job.step, job.material, PROFILE).user
    assert MOOD_RULE in user and AGREE in user and SONG in user


def test_talk_that_does_not_follow_a_song_leaves_the_reactions_alone(tmp_path):
    """이야기 도중에 전하면 하던 이야기에 끼어든다 — 방송 40개에서 30번 중 13번이 지적됐다 (T-017)."""
    scheduler, _ = _with_reactions(tmp_path)
    job = _opener(scheduler, after_song=False)
    assert job.material.mood == [] and len(scheduler.pool.reactions()) == 2
    run = _run(scheduler)
    scheduler.pool.pop("story")
    assert scheduler._talk_point(run) != {"source": "chat"}, "반응만으로는 이야기 단계를 열지 않는다"
    spec = scheduler.fmt.corner("music_intro")
    intro = asyncio.run(scheduler._build_job(PlannedStep(spec, spec.steps[0], [dict(TRACK)], None)))
    assert intro.material.mood == [] and len(scheduler.pool.reactions()) == 2, "곡 소개에서도 전하지 않는다"


def test_answer_ment_takes_answers_only_and_the_slot_is_then_gone(tmp_path):
    """질문의 답을 받는 차례에는 곡 감상도 맞장구도 뭉쳐 전하지 않는다 (2026-11-19 결정)."""
    scheduler, states = _with_reactions(tmp_path)
    before = _chat("와 표현 너무 좋다 ㅠㅠ", "before", about=ABOUT_TALK, at=time.time() - 200,
                   anchor_id="topic:0", anchor_seq=0)
    before.stale = True
    scheduler.pool.add(before)
    job = _opener(scheduler, after_song=True, asked=True)
    assert job.material.extra["asked"] == QUESTION and job.material.mood == []
    assert {q.request_id for q in job.material.quotes} == {"agree", "story"}, \
        "질문 뒤에 온 글은 맞장구도 답 후보다 — 곡 감상과 질문 전에 온 글은 아니다"
    assert job.material.extra["optional_quotes"]
    assert len(scheduler.pool) == 0, "이 자리를 놓친 반응은 받지 않는다"
    assert {rid for rid, state in states if state == RequestState.EXPIRED} == {"song", "before"}


def test_unpicked_candidate_goes_back_only_if_it_stands_alone(tmp_path):
    scheduler, states = _with_reactions(tmp_path)
    agree, story = scheduler.pool.pop("agree"), scheduler.pool.pop("story")
    job = GenerationJob(job_id="j", corner_type="topic_talk", step="talk_resume",
                        material=Material(quotes=[agree, story], extra={"optional_quotes": True}))

    class Nothing:
        async def run(self, job):
            from onair_engine.domain import SegmentKind, SegmentSubmission
            return SegmentSubmission(id="seg_a", station_id="s", audio_ref="a", duration_ms=5000,
                                     corner_type="topic_talk", kind=SegmentKind.SPEECH, priority=0,
                                     reorderable=False)

        def take_aired(self, _id):
            return None

    scheduler.pipeline = Nothing()

    async def scenario():
        seq = scheduler._reserve()
        await scheduler._generate(job, seq)

    asyncio.run(scenario())
    assert ("agree", RequestState.EXPIRED) in states, "질문에 기대는 글은 답을 받는 자리가 지나면 받지 않는다"
    assert [r.request_id for r in scheduler.pool.items if r.request_id in ("agree", "story")] == ["story"]


def test_ment_after_the_song_waits_for_the_song_when_listeners_are_chatting(tmp_path):
    """두면 곡 소개와 함께 곡 뒤 멘트까지 미리 만든다 — 곡이 나가는 동안 온 반응을 싣지 못한다."""
    scheduler, _ = _asking(tmp_path)
    scheduler.target_buffer_sec = 30
    now = time.time()
    assert not scheduler._waits_for_song(now), "소개한 곡이 없다"
    spec = scheduler.fmt.corner("music_intro")
    asyncio.run(scheduler._build_job(PlannedStep(spec, spec.steps[0], [dict(TRACK)], None)))
    anchor_id, _ = scheduler._song_hold
    assert not scheduler._waits_for_song(now), "조용한 방송은 받을 반응이 없다 — 기다리지 않는다"
    scheduler._last_chat_at = now
    assert scheduler._waits_for_song(now), "곡 소개가 아직 제출되지 않았다 — 잠깐 기다린다"
    assert not scheduler._waits_for_song(now + 31)
    anchor = scheduler._last_anchor
    assert anchor.id == anchor_id
    scheduler.timeline.append("seg_i", anchor, 10_000, now, track_ref="yt:abc", track_ms=120_000)
    scheduler._last_chat_at = now + 100
    assert scheduler._waits_for_song(now + 99) and not scheduler._waits_for_song(now + 101), \
        "곡이 끝나기 30초(target_buffer_sec) 전부터 만든다"
    scheduler._talk_comes_next = lambda: False
    assert not scheduler._waits_for_song(now + 60), "곡 뒤 차례가 이야기 코너가 아니면 기다리지 않는다"


def test_song_whose_intro_did_not_air_is_not_waited_for(tmp_path):
    """곡 소개를 LLM이 거부하면 곡도 나가지 않는다 — 기다리면 곡 뒤 멘트가 늦어 무음이 난다 (T-017 본 실행)."""
    scheduler, _ = _asking(tmp_path)
    scheduler._last_chat_at = time.time()
    spec = scheduler.fmt.corner("music_intro")
    first = asyncio.run(scheduler._build_job(PlannedStep(spec, spec.steps[0], [dict(TRACK)], None)))
    played = scheduler._song_hold
    scheduler.timeline.append("seg_i", first.anchor, 10_000, time.time() - 200, track_ref="yt:abc", track_ms=120_000)
    refused = asyncio.run(scheduler._build_job(PlannedStep(spec, spec.steps[0], [dict(TRACK, id=2)], None)))
    assert scheduler._song_hold[0] == refused.anchor.id and scheduler._waits_for_song(time.time())

    class Refuses:
        async def run(self, job):
            return None

    scheduler.pipeline = Refuses()

    async def scenario():
        await scheduler._generate(refused, scheduler._reserve())

    asyncio.run(scenario())
    assert scheduler._song_hold == played, "앞 곡으로 되돌린다 — 반응이 곡 하나만 넘는 것은 그대로 지킨다"
    assert not scheduler._waits_for_song(time.time())


def test_reactions_cross_one_song_only(tmp_path):
    """곡 뒤에 이야기 없이 또 곡이 오면, 앞 곡 전에 온 반응은 두 곡 뒤에야 전해진다 — 받지 않는다."""
    scheduler, states = _with_reactions(tmp_path)
    spec = scheduler.fmt.corner("music_intro")
    asyncio.run(scheduler._build_job(PlannedStep(spec, spec.steps[0], [dict(TRACK)], None)))
    assert len(scheduler.pool.reactions()) == 2, "첫 곡은 넘긴다 — 곡 뒤 첫 멘트가 전한다"
    scheduler.pool.add(_chat(SONG, "song2", about=ABOUT_SONG, anchor_id="track:2", anchor_seq=1))
    asyncio.run(scheduler._build_job(PlannedStep(spec, spec.steps[0], [dict(TRACK)], None)))
    assert [r.request_id for r in scheduler.pool.reactions()] == ["song2"]
    assert {rid for rid, state in states if state == RequestState.EXPIRED} == {"agree", "song"}


# ── 프롬프트와 인용 검사 ───────────────────────────────────────────────

def test_mood_prompt_names_what_the_reactions_were_about():
    """ "그때 나간 말에는…", "그 노래를 들으며…"는 무엇에 대한 반응인지 알 수 없다 (T-017)."""
    agree = _chat(AGREE, about=ABOUT_TALK)
    agree.heard = "곡: 새벽 버스 - 모의 밴드\n곡 전에 한 말: 오늘 밤의 간식도 서두르지 않고 천천히 가요."
    extra = {"after_song": dict(TRACK), "no_question": True}
    user = build_prompt("talk_resume", Material(topic="겨울 간식", mood=[agree], extra=extra), PROFILE).user
    assert MOOD_RULE in user and MOOD_HEARD_RULE in user and "서두르지 않고" in user
    assert "가리키기만 하는 말은 쓰지 않는다" in MOOD_HEARD_RULE and "분명하지 않은 반응은 전하지 않는다" in MOOD_HEARD_RULE
    asked = build_prompt("talk_resume", Material(topic="겨울 간식", extra={"asked": QUESTION}), PROFILE).user
    assert ASKED_ALONE_RULE in asked


def test_question_is_quoted_with_what_it_is_about():
    assert "무엇을 두고 묻는지" in QUOTE_RULE


def test_paraphrased_candidate_counts_as_taken():
    """풀어서 받은 답을 받지 않은 것으로 보고 되돌려, 다음 멘트가 같은 글을 또 받았다 (T-017)."""
    chat = "저는 무조건 야채요ㅋㅋ 종이 껍질 뜯을 때 붙은 만두피 갉아먹는 게 찐맛이에요 ㅠㅠ"
    script = "곡 전에는 호빵 속 취향을 여쭤봤습니다. 무조건 야채라고 하신 분, 만두피까지 챙겨 드시는 재미가 있겠네요."
    assert not quoted(chat, script) and mentioned(chat, script)
    other = "곡 전에는 호빵 속 취향을 여쭤봤습니다. 저는 팥이 든 호빵을 좋아해요."
    assert not mentioned(chat, other)
    assert not mentioned("내일 출근하기 싫어요", "어느 쪽이 좋은지 여쭤봤죠. 저는 호빵이요라고 하신 분, 든든한 선택이네요.")


def test_words_of_the_question_do_not_count_as_a_mention():
    """답 후보와 대본은 질문의 낱말을 어차피 함께 쓴다 (본 실행 20261119-221335, T-017)."""
    question = "군고구마는 손에 쥐고 먹기 좋은 작은 쪽을 고르시나요, 나눠 먹기 좋은 큰 쪽을 고르시나요?"
    chat = "전 무조건 큰 거 골라서 집에 가자마자 엄마랑 반 갈라서 김치 올려 먹어요 ㅋㅋ"
    script = ("아까는 군고구마 크기를 어떻게 고르는지 나눴죠. 작은 게 편하다고 하신 분, 걷는 길에도 잘 어울리겠어요. "
              "큰 걸 골라 어머니와 나눠 먹는다고 하신 분, 김치까지 곁들이면 더 맛있게 남겠네요.")
    assert mentioned(chat, script, question), "질문에 없는 낱말 둘(골라, 김치)이 겹친다 — 풀어서 받았다"
    asked = "함께 나눌 때, 먼저 건네고 싶은 겨울 간식은 무엇인가요?"
    unread = "새벽님은 친구랑 간식 나눠 먹을 때 보통 양보하는 편이세요? 아니면 먼저 고르세요?"
    own = "함께 나눌 때 먼저 건네고 싶은 간식을 여쭈었지요. 저는 각자 좋아하는 걸 하나씩 골라 가운데 놓고 싶어요. 나눠 먹는 맛에 보태지니까요."
    assert not mentioned(unread, own, asked), "DJ가 자기 답만 했다 — 겹친 것은 질문의 낱말뿐이다"
