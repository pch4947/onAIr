"""반응형 채팅은 한 건씩 인용하지 않고 분위기로만 전한다 (CORNER_DESIGN 5.1.3절, 2026-11-15 결정).

실제 방송에서 "오 그 말 들어보니까 맞는 것 같아요"가 다른 이야기를 하던 멘트에 인용돼, 그 이야기에 대한
반응처럼 들렸다. 엔진은 방송보다 앞서 멘트를 만들기 때문이다.
"""
import asyncio
import time
from pathlib import Path

import pytest

from onair_engine.corners.run import PlannedStep
from onair_engine.corners.steps import (
    MOOD_HEARD_RULE,
    MOOD_RULE,
    QUOTE_HEARD_RULE,
    QUOTE_RULE,
    build_prompt,
)
from onair_engine.domain import (
    Anchor,
    ChatType,
    GenerationJob,
    ListenerRequest,
    Material,
    Prompt,
    Script,
)
from onair_engine.inputs.classify import classify
from onair_engine.inputs.on_air import OnAirTimeline
from onair_engine.inputs.pool import QuotePool, is_mood
from onair_engine.inputs.rules import load_chat_rules
from onair_engine.pipeline.llm import detect_step
from onair_engine.pipeline.pipeline import GenerationPipeline
from onair_engine.pipeline.safety import SafetyChecker
from onair_engine.pipeline.tts import make_tts
from onair_engine.prompt_data import CHAT, HEARD, REACTIONS
from onair_engine.telemetry import Telemetry
from test_scheduler import PROFILE
from test_talk_material import _scheduler_run

ROOT = Path(__file__).resolve().parent.parent
RULES = load_chat_rules(ROOT / "config" / "chat_rules.yaml")
AGREE = "오 그 말 들어보니까 맞는 것 같아요"


def _chat(body: str, rid: str = "r1", who: str = "u1", chat_type: ChatType | None = None,
          heard: str | None = None, rule: str | None = None, back_ref: bool = False) -> ListenerRequest:
    if chat_type == ChatType.REACTION and rule is None:
        rule = "pointing"
    return ListenerRequest(request_id=rid, kind="chat", body=body, requester_ref=who, received_at=time.time(),
                           chat_type=chat_type, rule=rule, heard=heard, back_ref=back_ref)


# ── 무엇이 분위기로 가는가 ─────────────────────────────────────────────

@pytest.mark.parametrize(("body", "chat_type"), [
    (AGREE, ChatType.REACTION),  # 방금 한 말을 가리킨다
    ("그 얘기 완전 공감돼요", ChatType.REACTION),
    ("맞아요 맞아요", ChatType.REACTION),
    ("저는 수능 날 도시락으로 김밥 싸 갔어요", ChatType.STANDALONE),  # 자기 이야기
])
def test_reactions_to_what_was_said_are_classified_as_reactions(body, chat_type):
    assert classify(body, None, RULES)[0] == chat_type


def test_only_reactions_without_a_question_go_to_mood():
    assert is_mood(_chat(AGREE, chat_type=ChatType.REACTION))
    assert not is_mood(_chat("방금 그 말 무슨 뜻이에요?", chat_type=ChatType.REACTION)), "질문은 답해야 한다"
    assert not is_mood(_chat("저는 김밥 싸 갔어요", chat_type=ChatType.STANDALONE))
    assert is_mood(_chat("대박 ㅋㅋ", chat_type=ChatType.REACTION, rule="short_reaction"))
    # 화제 낱말이 든 채팅은 글만으로 뜻이 통한다 — 한 건씩 인용한다
    assert not is_mood(_chat("저는 수능 날 김밥 싸 갔어요", chat_type=ChatType.REACTION, rule="anchor_keyword"))


# ── 그때 방송에 나가던 것 ──────────────────────────────────────────────

def test_timeline_knows_what_the_listener_was_hearing():
    timeline = OnAirTimeline()
    anchor = Anchor(id="topic:1", kind="topic", keywords=frozenset(), seq=0)
    timeline.append("s1", anchor, 10_000, 100.0, text="도시락 이야기를 해 볼게요.")
    timeline.append("s2", anchor, 2_000, 100.0)  # 접착 문구 — 대본을 모른다
    timeline.append("s3", anchor, 5_000, 100.0, track_ref="t1", track_ms=60_000,
                    text="다음 곡 들려드릴게요.", track="밤편지 — 아이유")
    assert timeline.heard(99.0) is None
    assert timeline.heard(105.0) == "도시락 이야기를 해 볼게요."
    assert timeline.heard(111.0) == "도시락 이야기를 해 볼게요.", "대본을 모르는 항목이면 그 앞 멘트를 본다"
    assert timeline.heard(113.0) == "다음 곡 들려드릴게요."
    # 곡이 나가는 동안의 반응은 곡 전에 한 말에 대한 것일 수 있다 — 둘 다 알려 준다
    assert timeline.heard(130.0) == "곡: 밤편지 — 아이유\n곡 전에 한 말: 도시락 이야기를 해 볼게요. 다음 곡 들려드릴게요."


# ── 프롬프트 ───────────────────────────────────────────────────────────

def test_mood_prompt_carries_reactions_without_asking_for_quotes():
    mood = [_chat(AGREE, chat_type=ChatType.REACTION, heard="도시락은 든든한 게 최고라는 얘기였어요."),
            _chat("저도 그렇게 생각해요", rid="r2", chat_type=ChatType.REACTION)]
    user = build_prompt("talk", Material(topic="수능 응원", mood=mood), PROFILE).user
    assert detect_step(user) == "talk"
    assert MOOD_RULE in user and QUOTE_RULE not in user and f'<data label="{CHAT}">' not in user
    assert f'<data label="{REACTIONS}">\n{AGREE}\n저도 그렇게 생각해요\n</data>' in user
    assert MOOD_HEARD_RULE in user and f'<data label="{HEARD}">\n도시락은 든든한 게 최고라는 얘기였어요.\n</data>' in user


def test_mood_prompt_without_known_context_and_alongside_a_quote():
    quote = _chat("저는 김밥 싸 갔어요", rid="q1", chat_type=ChatType.STANDALONE)
    mood = [_chat("맞아요 맞아요", chat_type=ChatType.REACTION)]
    user = build_prompt("talk", Material(topic="수능 응원", quotes=[quote], mood=mood), PROFILE).user
    assert QUOTE_RULE in user and MOOD_RULE in user and MOOD_HEARD_RULE not in user
    assert f'<data label="{CHAT}">\n저는 김밥 싸 갔어요\n</data>' in user
    with pytest.raises(ValueError, match="반응"):
        build_prompt("text_read", Material(items=[{"body": "문자"}], mood=mood), PROFILE)


def test_question_about_what_was_said_carries_what_was_on_air():
    """되묻는 채팅은 무엇을 가리키는지 알려 줘야 답을 지어내지 않는다."""
    ask = _chat("그게 무슨 말이에요?", rid="q1", back_ref=True, heard="알고 있는 것을 제자리에 두는 시간이에요.")
    own = _chat("저는 김밥 싸 갔어요", rid="q2", heard="알고 있는 것을 제자리에 두는 시간이에요.")
    user = build_prompt("talk", Material(topic="수능 응원", quotes=[ask]), PROFILE).user
    assert QUOTE_HEARD_RULE in user
    assert f'<data label="{HEARD}">\n알고 있는 것을 제자리에 두는 시간이에요.\n</data>' in user
    plain = build_prompt("talk", Material(topic="수능 응원", quotes=[own]), PROFILE).user
    assert QUOTE_HEARD_RULE not in plain and HEARD not in plain, "자기 이야기에는 붙이지 않는다"


def test_mood_reactions_outlive_the_reaction_window_and_anchor_changes():
    """무엇에 대한 반응인지 함께 전하므로 곡 하나를 지나서 전해도 된다 — 60초 창으로는 대부분 만료됐다 (T-012)."""
    pool = QuotePool(reaction_window_sec=60, standalone_window_sec=600, track_end=lambda anchor_id: None,
                     mood_window_sec=300)
    agree = _chat(AGREE, chat_type=ChatType.REACTION)
    agree.received_at, agree.anchor_seq = 1000.0, 3
    on_topic = _chat("저는 수능 날 김밥 싸 갔어요", rid="r2", chat_type=ChatType.REACTION, rule="anchor_keyword")
    on_topic.received_at, on_topic.anchor_seq = 1000.0, 3
    pool.add(agree)
    pool.add(on_topic)
    assert [r.request_id for r in pool.expire(1200.0, 6)] == ["r2"], "한 건씩 인용하는 반응은 예전 그대로 만료된다"
    assert pool.expire(1301.0, 6) == [agree]


# ── 편성 ──────────────────────────────────────────────────────────────

def _talk_job(scheduler, run) -> GenerationJob:
    corner = scheduler.fmt.corner("topic_talk")
    step = next(s for s in corner.steps if s.kind == "talk")
    return asyncio.run(scheduler._build_job(PlannedStep(corner, step, [{"source": "chat"}], None)))


def test_reaction_is_told_as_mood_and_own_story_is_quoted(tmp_path):
    scheduler, run = _scheduler_run(tmp_path, None)
    scheduler.enqueue_request(_chat(AGREE, rid="react"))
    scheduler.enqueue_request(_chat("저는 수능 날 도시락으로 김밥 싸 갔어요", rid="own", who="u2"))
    assert scheduler._talk_point(run) == {"source": "chat"}
    job = _talk_job(scheduler, run)
    assert [q.request_id for q in job.material.quotes] == ["own"]
    assert [m.request_id for m in job.material.mood] == ["react"]
    assert scheduler.pending_count == 0


def test_reactions_alone_open_a_talk_and_are_spaced_out(tmp_path):
    scheduler, run = _scheduler_run(tmp_path, None)
    scheduler.enqueue_request(_chat("맞아요 맞아요", rid="a"))
    scheduler.enqueue_request(_chat("그 얘기 공감돼요", rid="b", who="u2"))
    assert scheduler._talk_point(run) == {"source": "chat"}, "반응만 와 있어도 이야기 단계를 연다"
    job = _talk_job(scheduler, run)
    assert job.material.quotes == [] and {m.request_id for m in job.material.mood} == {"a", "b"}
    assert job.material.extra["material"] == "topic", "인용할 채팅이 없으면 다른 소재로 말하고 분위기를 붙인다"

    scheduler.enqueue_request(_chat("저도요 ㅋㅋ", rid="c", who="u3"))
    assert _talk_job(scheduler, run).material.mood == [], "방금 분위기를 전했다 — 멘트마다 붙이지 않는다"
    scheduler._last_mood_at -= scheduler.chat_rules.mood_gap_sec
    assert [m.request_id for m in _talk_job(scheduler, run).material.mood] == ["c"]


def test_question_reaction_is_still_quoted_one_by_one(tmp_path):
    scheduler, run = _scheduler_run(tmp_path, None)
    scheduler.enqueue_request(_chat("방금 그 말 무슨 뜻이에요?", rid="ask"))
    job = _talk_job(scheduler, run)
    assert [q.request_id for q in job.material.quotes] == ["ask"] and job.material.mood == []
    assert job.material.quotes[0].back_ref, "방금 들은 말을 가리킨다 — 그때 나가던 말을 프롬프트에 넣는다"
    assert not job.material.extra.get("steer"), "되묻는 채팅은 화제로 삼지 않는다"


# ── 파이프라인 ────────────────────────────────────────────────────────

class Scripted:
    def __init__(self, text: str):
        self.text = text

    async def generate(self, prompt: Prompt) -> Script:
        return Script(text=self.text)


def test_mood_inputs_are_reflected_without_a_quote_check(tmp_path):
    """분위기로 전한 반응은 대본에 글이 그대로 들어가지 않는다 — 인용 검사를 하지 않고 반영으로 싣는다."""
    pipeline = GenerationPipeline(
        station_id="st_test", profile=PROFILE, llm=Scripted("방금 이야기에 고개를 끄덕여 주신 분이 계시네요."),
        tts=make_tts("dummy"), safety=SafetyChecker(ROOT / "config" / "safety_rules.yaml"),
        telemetry=Telemetry(tmp_path / "metrics.sqlite", "st_test"), audio_root=tmp_path / "audio")
    mood = [_chat(AGREE, rid="react", chat_type=ChatType.REACTION)]
    job = GenerationJob(job_id="job_m", corner_type="topic_talk", step="talk",
                        material=Material(topic="수능 응원", mood=mood))
    sub = asyncio.run(pipeline.run(job))
    pipeline.telemetry.close()
    assert sub is not None and sub.input_refs == ["react"]
