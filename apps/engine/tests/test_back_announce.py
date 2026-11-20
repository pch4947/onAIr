"""곡이 끝나면 방금 나간 곡부터 알리고, 엔딩 돌아보기는 주제에 관한 글만 꺼낸다 (2026-11-20 결정, T-020).

실제 방송에서 곡이 끝나자마자 다음 이야기로 넘어갔고, 엔딩이 "노래가 끊겼어요", "왜 말씀이 없으세요"를 돌아봤다.
"""
import asyncio
import time

import pytest

from onair_engine.corners.steps import (
    BACK_ANNOUNCE_RULE,
    BACK_ANNOUNCE_THEN_RULE,
    BLOCK_STEP,
    MOOD_AFTER_ANNOUNCE_RULE,
    MOOD_RULE,
    PREV_TRACK,
    announces_song,
    build_prompt,
)
from onair_engine.domain import TONE_PLAIN, GenerationJob, ListenerRequest, Material
from onair_engine.inputs.callbacks import CallbackStore
from onair_engine.inputs.chat_tone import ChatToneJudge, parse_on_topic
from onair_engine.inputs.chat_tone import build_prompt as tone_prompt
from test_listener_talk import QUESTION, TRACK, ToneLlm, _run
from test_scheduler import PROFILE, _engine

SONG_BLOCK = f'<data label="{PREV_TRACK}">'


def _chat(body: str, rid: str = "c1", **fields) -> ListenerRequest:
    return ListenerRequest(request_id=rid, kind="chat", body=body, requester_ref=rid, received_at=time.time(), **fields)


def _after_song(**extra) -> Material:
    return Material(topic="겨울 간식", extra={"after_song": dict(TRACK), "back_announce": True, **extra})


# ── 곡을 알리는 말 ─────────────────────────────────────────────────────

@pytest.mark.parametrize(("step", "first"), [
    ("talk_open", "첫 문장은"), ("talk_resume", "첫 문장은"), (BLOCK_STEP, "첫 문단의 첫 문장은")])
def test_first_ment_after_a_song_names_the_song(step, first):
    material = _after_song(parts=4)
    user = build_prompt(step, material, PROFILE).user
    assert BACK_ANNOUNCE_RULE.format(first=first) in user and user.count(SONG_BLOCK) == 1
    assert TRACK["title"] in user
    assert BACK_ANNOUNCE_THEN_RULE not in user, "받을 채팅이 없으면 순서를 말하지 않는다"


def test_answer_turn_only_takes_the_answers():
    """곡 전에 던진 질문으로 돌아오는 멘트는 곡을 알리지 않는다 — 답만 받는다 (7.7.2절, 사용자 결정)."""
    material = _after_song(asked=QUESTION)
    user = build_prompt("talk_resume", material, PROFILE).user
    assert not announces_song(material)
    assert "방금 나간 곡이 무엇이었는지" not in user and SONG_BLOCK not in user


def test_chats_and_reactions_come_right_after_the_song_is_named():
    quoted = _after_song()
    quoted.quotes = [_chat("저는 붕어빵은 팥이에요")]
    assert BACK_ANNOUNCE_THEN_RULE in build_prompt("talk_resume", quoted, PROFILE).user
    moody = _after_song()
    moody.mood = [_chat("노래 너무 포근하네요", heard="곡: 새벽 버스 — 모의 밴드")]
    user = build_prompt("talk_resume", moody, PROFILE).user
    assert BACK_ANNOUNCE_THEN_RULE in user and MOOD_AFTER_ANNOUNCE_RULE in user and MOOD_RULE not in user
    plain = Material(topic="겨울 간식", mood=moody.mood)
    assert MOOD_RULE in build_prompt("talk", plain, PROFILE).user, "곡을 알리지 않는 멘트에서는 첫머리에 전한다"


def test_song_story_carries_the_song_once():
    material = _after_song(parts=4, song_story=dict(TRACK))
    user = build_prompt(BLOCK_STEP, material, PROFILE).user
    assert "방금 나간 곡을 듣고 떠오른 이야기" in user and user.count(SONG_BLOCK) == 1


@pytest.mark.parametrize(("every", "expected"), [(1, [True, True, True]), (2, [True, False, True]), (0, [False] * 3)])
def test_how_often_the_song_is_named_is_a_setting(tmp_path, every, expected):
    engine, _ = _engine(tmp_path, corners=["topic_talk"], topic="겨울 간식")
    scheduler = engine.scheduler
    scheduler.chat_rules = scheduler.chat_rules.model_copy(update={"back_announce_every": every})
    scheduler._run = _run(scheduler)
    scheduler._run.after_song = True
    scheduler._last_track = dict(TRACK)
    seen = []
    for _ in expected:
        material = Material(topic="겨울 간식")
        scheduler._open_thread(material)
        seen.append(material.extra["back_announce"])
    engine.telemetry.close()
    assert seen == expected


# ── 엔딩 돌아보기는 주제에 관한 글만 ─────────────────────────────────────

def test_judge_also_says_whether_the_chat_is_about_the_topic():
    assert parse_on_topic('{"tone": "plain", "about": "self", "topic": "on"}') is True
    assert parse_on_topic('{"tone": "plain", "about": "self", "topic": "off"}') is False
    assert parse_on_topic('{"tone": "plain", "about": "self"}') is None, "답이 없으면 판정하지 않은 것으로 본다"
    assert "셋째(topic)" in tone_prompt("겨울 간식", "노래가 끊겼어요").user
    chat = _chat("노래가 끊겼어요")
    llm = ToneLlm('{"tone": "plain", "about": "self", "topic": "off"}')
    assert asyncio.run(ChatToneJudge(llm, "겨울 간식").judge(chat)) == TONE_PLAIN
    assert chat.on_topic is False
    untitled = _chat("노래가 끊겼어요")
    asyncio.run(ChatToneJudge(llm, "").judge(untitled))
    assert untitled.on_topic is None, "주제 없는 방송은 판정하지 않는다"


def test_lookback_keeps_only_chats_about_the_topic():
    store = CallbackStore()
    store.add(_chat("노래가 끊겼어요", "c1", on_topic=False), 0.0)
    store.add(_chat("왜 말씀이 없으세요", "c2", on_topic=False), 1.0)
    store.add(_chat("저는 붕어빵은 무조건 팥이에요", "c3", on_topic=True), 2.0)
    store.add(_chat("어릴 때 화로에 구워 먹던 가래떡이 생각나요", "c4"), 3.0)  # 판정하지 않은 글은 예전 규칙대로
    assert [a.body for a in store.lookback()] == ["저는 붕어빵은 무조건 팥이에요", "어릴 때 화로에 구워 먹던 가래떡이 생각나요"]
    letter = ListenerRequest(request_id="m1", kind="message", body="오늘 하루 고생했어요", requester_ref="u",
                             received_at=0.0, nickname="밤샘러")
    store.add(letter, 4.0)
    assert len(store) == 3, "문자·사연은 판정하지 않는다 — 그대로 돌아본다"


def test_off_topic_chat_that_aired_is_not_looked_back_on(tmp_path):
    engine, _ = _engine(tmp_path, corners=["topic_talk"])
    scheduler = engine.scheduler
    job = GenerationJob(job_id="j", corner_type="topic_talk", step="talk", material=Material())
    scheduler._reflect(_chat("노래가 끊겼어요", "c1", on_topic=False), job, "seg_1", time.time())
    scheduler._reflect(_chat("저는 호빵파예요", "c2", on_topic=True), job, "seg_2", time.time())
    engine.telemetry.close()
    assert [a.body for a in scheduler.callbacks.lookback()] == ["저는 호빵파예요"]
