"""청취자가 적은 방송의 흐름 — 곡 뒤 이어 말하기, 한 번에 쓴 이야기 덩어리, 질문 줄이기 (CORNER_DESIGN 7.7.1절, #262)."""
import asyncio
import sqlite3
import time
from pathlib import Path

from onair_engine.corners.run import CornerRun, Thread
from onair_engine.corners.steps import (
    ANGLE_CONTINUE_RULE,
    BLOCK_MARKER,
    BLOCK_STEP,
    NO_QUESTION_RULE,
    PREV_TRACK,
    build_prompt,
    split_block,
)
from onair_engine.domain import BlockScript, GenerationJob, ListenerRequest, Material, SegmentKind
from onair_engine.format.clock import ClockEntry
from onair_engine.pipeline.llm import REJECT, DummyLlmClient, LlmError, detect_step
from onair_engine.pipeline.pipeline import GenerationPipeline
from onair_engine.pipeline.safety import SafetyChecker
from onair_engine.prompt_data import ANGLE, TRACK
from onair_engine.telemetry import Telemetry
from test_scheduler import PROFILE, RULES, _engine, _glue


class RecordingLlm:
    def __init__(self, inner=None):
        self.inner = inner or DummyLlmClient(delay_sec=0)
        self.generated, self.completed = [], []

    async def generate(self, prompt):
        self.generated.append(prompt)
        return await self.inner.generate(prompt)

    async def complete(self, prompt, *, max_tokens):
        self.completed.append(prompt)
        return await self.inner.complete(prompt, max_tokens=max_tokens)


def _blocks(llm: RecordingLlm) -> list[str]:
    return [p.user for p in llm.completed if BLOCK_MARKER in p.user]


def _quiet_broadcast(tmp_path, segments: int):
    engine, transport = _engine(tmp_path, block=True, corners=["topic_talk"], topic="겨울 간식")
    engine.scheduler.pipeline.llm = llm = RecordingLlm()
    engine.topic_plan._llm.delay_sec = 0
    asyncio.run(engine.run(max_segments=segments))
    engine.telemetry.close()
    return engine, transport, llm


# ── 문단 나누기 ────────────────────────────────────────────────────────

def test_split_block_turns_paragraphs_into_segments():
    raw = "첫 문단이에요. 둘째 문장.\n\n**둘째 문단**입니다. 이어서요.\n \n셋째 문단.\n\n넷째.\n\n다섯째."
    assert split_block(raw, 4) == ["첫 문단이에요. 둘째 문장.", "둘째 문단입니다. 이어서요.", "셋째 문단.",
                                   "넷째. 다섯째."], "계획보다 많은 문단은 마지막에 붙인다"
    # 빈 줄 없이 한 문단으로 오면 문장 수로 고르게 나눈다
    assert split_block("하나예요. 둘이에요. 셋이에요. 넷이에요. 다섯이에요. 여섯이에요.", 3) == [
        "하나예요. 둘이에요.", "셋이에요. 넷이에요.", "다섯이에요. 여섯이에요."]
    assert split_block("REJECT", 3) == [REJECT] and split_block(" ** ", 3) == []


def test_block_prompt_is_its_own_step():
    m = Material(topic="겨울 간식", extra={"parts": 4, "no_question": True})
    user = build_prompt(BLOCK_STEP, m, PROFILE).user
    assert detect_step(user) == BLOCK_STEP and "문단 4개" in user and NO_QUESTION_RULE in user
    m.extra.update(question=True, after_song={"title": "새벽 버스", "artist": "모의 밴드"}, back_announce=True)
    user = build_prompt(BLOCK_STEP, m, PROFILE).user
    assert detect_step(user) == BLOCK_STEP and NO_QUESTION_RULE not in user and "마지막 문단 끝에만" in user
    assert f'<data label="{PREV_TRACK}">\n곡: 새벽 버스 — 모의 밴드\n</data>' in user
    for step in ("talk_open", "talk_resume"):  # 한 번에 쓰지 않는 흐름의 꺼내는 멘트도 곡 뒤를 잇는다
        user = build_prompt(step, m, PROFILE).user
        assert detect_step(user) == step and PREV_TRACK in user


# ── 방송 ──────────────────────────────────────────────────────────────

def test_quiet_broadcast_writes_each_break_in_one_call(tmp_path):
    engine, transport, llm = _quiet_broadcast(tmp_path, segments=14)
    blocks = _blocks(llm)
    assert len(blocks) == 2, "곡과 곡 사이 말은 LLM 한 번으로 쓴다"
    assert not [p for p in llm.generated if detect_step(p.user) in ("talk_open", "talk_resume", "talk")]
    # 첫 진입: 문단 넷이 세그먼트 넷으로 차례대로 나간다
    talk = [s for s in transport.segments if s.corner_type == "topic_talk" and not _glue(s)]
    scripts = engine.scheduler.pipeline._pending_air
    first = [scripts[s.id] for s in talk[:4]]
    assert first[0].startswith("오늘 이야기 조금 더 이어가 볼게요") and first[3].startswith("그래서 오늘은")
    assert len(set(first)) == 4
    # 둘째 진입: 곡만 사이에 있었다 — 돌아왔다는 문구 없이 방금 곡을 받아 하던 갈래를 잇는다
    glue = [s.audio_ref.rsplit("/", 1)[1].rsplit("_", 1)[0] for s in transport.segments if _glue(s)]
    assert glue.count("topic_talk_call") == 1 and "topic_talk_resume" not in glue
    assert "질문을 하나 던진다" in blocks[0] and PREV_TRACK not in blocks[0]
    assert PREV_TRACK in blocks[1] and ANGLE_CONTINUE_RULE in blocks[1]
    assert NO_QUESTION_RULE in blocks[1], "답이 없는 방송에서 곡마다 묻지 않는다"
    # 갈래 하나(말할 거리 넷)를 두 번에 걸쳐 쓴다
    assert "- 떠오르는 장면 하나\n- 흔히 겪는 일 하나\n- 둘 중 하나 고르기" in blocks[0]
    assert "- 마무리하는 생각 하나" in blocks[1] and "떠오르는 장면" not in blocks[1]
    db = sqlite3.connect(tmp_path / "metrics.sqlite")
    materials = [m for (m,) in db.execute(
        "SELECT json_extract(decision_json, '$.material') FROM decision_log "
        "WHERE json_extract(decision_json, '$.action') = 'generate'") if m]
    assert materials.count("block") == 8, "문단마다 생성 결정이 하나씩 남는다"


def test_every_third_quiet_break_talks_about_the_next_song(tmp_path):
    _, _, llm = _quiet_broadcast(tmp_path, segments=22)
    third = _blocks(llm)[2]
    assert "이어서 나갈 곡" in third and f'<data label="{TRACK}">' in third
    assert ANGLE not in third and PREV_TRACK not in third, "주제 갈래와 방금 곡 받기는 쉰다"


def test_chat_keeps_the_broadcast_on_short_segments(tmp_path):
    engine, _ = _engine(tmp_path, block=True, corners=["topic_talk"], topic="겨울 간식")
    engine.scheduler.pipeline.llm = llm = RecordingLlm()

    async def scenario():
        await engine.submit_request("chat", "저는 호빵파예요", "u1")
        await engine.run(max_segments=7)

    asyncio.run(scenario())
    assert not _blocks(llm), "채팅이 오가는 동안은 멘트 하나씩 만들어 빨리 반영한다"
    assert [p for p in llm.generated if detect_step(p.user) == "talk_open"]


def _run(scheduler) -> CornerRun:
    run = CornerRun(ClockEntry(scheduler.fmt.corner("topic_talk")), sources=scheduler.sources,
                    intensity=scheduler.intensity, thread=Thread(topic="겨울 간식"))
    scheduler._run = run
    return run


def _chat(body: str, rid: str = "c1") -> ListenerRequest:
    return ListenerRequest(request_id=rid, kind="chat", body=body, requester_ref="u", received_at=time.time())


def test_chat_that_arrives_during_a_block_follows_it(tmp_path):
    engine, _ = _engine(tmp_path, block=True, corners=["topic_talk"])
    scheduler = engine.scheduler

    async def scenario():
        run = _run(scheduler)
        block = scheduler._plan_block(run.spec)
        assert block is not None and block.parts == 4 and run.block_next == 1
        scheduler.enqueue_request(_chat("저는 호빵파예요"))
        points = [scheduler._talk_point(run) for _ in range(4)]
        assert [(p["source"], p.get("part")) for p in points] == [
            ("block", 1), ("block", 2), ("block", 3), ("chat", None)], "쓴 문단을 다 내고 바로 뒤에 인용한다"
        assert scheduler._plan_block(run.spec) is None, "채팅이 있으면 한 번에 쓰지 않는다"
        # 문단을 다 낸 뒤에는 채팅만 더 붙는다. 채팅이 화제를 바꾸면 그 화제로는 이야기를 잇는다
        assert scheduler._quiet_point(run) is None
        run.thread = Thread(topic="호빵", free_points=2)
        assert scheduler._quiet_point(run) == {"source": "topic"}

    asyncio.run(scenario())


def test_quiet_broadcast_asks_listeners_only_now_and_then(tmp_path):
    engine, _ = _engine(tmp_path, block=True, corners=["topic_talk"])
    scheduler = engine.scheduler
    _run(scheduler)
    first, second = Material(), Material()
    scheduler._open_thread(first)
    scheduler._open_thread(second)
    assert first.extra["question"] is True and "no_question" not in first.extra, "처음에는 참여를 권한다"
    assert second.extra["question"] is False and second.extra["no_question"] is True
    scheduler._last_question_at -= scheduler.chat_rules.question_gap_sec
    again = Material()
    scheduler._open_thread(again)
    assert again.extra["question"] is True, "간격이 지나면 다시 묻는다"
    # 채팅이 오가면 예전처럼 꺼낼 때마다 묻는다
    scheduler.enqueue_request(_chat("저는 호빵파예요"))
    active = Material()
    scheduler._open_thread(active)
    assert active.extra["question"] is True and "no_question" not in active.extra


# ── 파이프라인 ─────────────────────────────────────────────────────────

class SlowTts:
    """합성이 대본 쓰기보다 훨씬 오래 걸린다 — 실제와 같다 (LLM 1.5초, TTS 7~14초)."""

    file_ext = ".wav"

    async def synthesize(self, text, out_path):
        await asyncio.sleep(0.2)
        return 1000


def _pipeline(tmp_path, llm) -> GenerationPipeline:
    return GenerationPipeline(station_id="st_test", profile=PROFILE, llm=llm, tts=SlowTts(),
                              safety=SafetyChecker(RULES), telemetry=Telemetry(tmp_path / "m.sqlite", "st_test"),
                              audio_root=Path(tmp_path) / "audio")


def _job(step: str, **kw) -> GenerationJob:
    return GenerationJob(job_id=f"job_{step}_{kw.get('part', 0)}", corner_type="topic_talk", step=step,
                         material=Material(topic="겨울 간식", extra={"parts": 3}), **kw)


def test_next_script_sees_the_one_before_it_without_waiting_for_tts(tmp_path):
    # 예전에는 합성이 끝나야 맥락에 들어가서, 동시에 만든 멘트가 앞 멘트를 모르고 같은 질문을 되풀이했다
    llm = RecordingLlm()
    pipeline = _pipeline(tmp_path, llm)

    async def scenario():
        return await asyncio.gather(pipeline.run(_job("talk_open")), pipeline.run(_job("talk")))

    first, second = asyncio.run(scenario())
    pipeline.telemetry.close()
    assert first is not None and second is not None
    assert pipeline._pending_air[first.id] in llm.generated[1].system, "둘째 멘트가 첫 멘트의 대본을 보고 쓴다"
    assert "마지막 줄이 방금 한 말" in llm.generated[1].system


def test_block_is_written_once_and_voiced_by_paragraph(tmp_path):
    llm = RecordingLlm()
    pipeline = _pipeline(tmp_path, llm)

    async def scenario():
        block = BlockScript(4)
        jobs = [_job(BLOCK_STEP, block=block, part=i) for i in range(4)]
        return await asyncio.gather(*(pipeline.run(j) for j in reversed(jobs)))  # 뒤 문단 작업이 먼저 기다린다

    subs = list(reversed(asyncio.run(scenario())))
    pipeline.telemetry.close()
    assert len(llm.completed) == 1 and not llm.generated
    # 더미는 요청한 문단 수(3)만큼 쓴다 — 넷째 작업은 낼 문단이 없어 건너뛴다
    assert [s is not None for s in subs] == [True, True, True, False]
    texts = [pipeline._pending_air[s.id] for s in subs[:3]]
    assert texts[0].startswith("오늘 이야기 조금 더") and len(set(texts)) == 3
    assert list(pipeline._recent) == texts, "문단이 차례대로 맥락에 들어간다"
    db = sqlite3.connect(tmp_path / "m.sqlite")
    stages = db.execute("SELECT stage, COUNT(*) FROM generation_log WHERE result = 'ok' GROUP BY stage").fetchall()
    assert dict(stages) == {"llm": 1, "l2": 1, "tts": 3}


class BrokenBlocks(RecordingLlm):
    async def complete(self, prompt, *, max_tokens):
        self.completed.append(prompt)
        raise LlmError("429")


def test_failed_block_falls_back_once_and_frees_the_waiting_parts(tmp_path):
    llm = BrokenBlocks()
    pipeline = _pipeline(tmp_path, llm)

    async def scenario():
        block = BlockScript(3)
        return await asyncio.wait_for(
            asyncio.gather(*(pipeline.run(_job(BLOCK_STEP, block=block, part=i)) for i in range(3))), timeout=5)

    first, second, third = asyncio.run(scenario())
    pipeline.telemetry.close()
    assert len(llm.completed) == 2, "한 번 다시 부른다"
    assert first.kind == SegmentKind.FILLER and second is None and third is None, "대체 멘트 하나만 나가고 방송은 이어진다"
