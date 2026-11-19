"""느린 LLM·TTS 호출에 예비 호출을 겹쳐 보낸다 (#340).

엔진은 방송 30초 분량만 쌓아 두는데 호출 하나의 상한이 LLM 20초, TTS 30초다. 실패한 뒤에야 다시 부르면
느린 호출 하나가 그대로 무음이 된다 (2026-11-20 실제 방송: TTS가 30초에 실패하고 다시 부른 것까지 62초).
"""
import asyncio

import pytest

from onair_engine.domain import GenerationJob, Material
from onair_engine.pipeline.pipeline import HEDGE_LLM_SEC, tts_hedge_sec
from onair_engine.pipeline.tts import TtsQuotaError
from test_scheduler import _engine

JOB = GenerationJob(job_id="j", corner_type="topic_talk", step="talk", material=Material())


class Calls:
    """부를 때마다 다음 계획대로 답한다 — (걸리는 초, 답 또는 예외)."""

    def __init__(self, *plan):
        self.plan = list(plan)
        self.started = 0
        self.cancelled = 0

    async def __call__(self):
        delay, result = self.plan[self.started]
        self.started += 1
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if isinstance(result, Exception):
            raise result
        return result


def _pipeline(tmp_path, **kwargs):
    engine, _ = _engine(tmp_path)
    pipeline = engine.scheduler.pipeline
    pipeline._hedge = kwargs.get("hedge", True)
    stages: list[str] = []
    pipeline._log = lambda job, stage, started, finished, result: stages.append(f"{stage}:{result}")
    return pipeline, stages


def _run(pipeline, calls, hedge_after=0.05, stage="tts"):
    return asyncio.run(pipeline._retry(JOB, stage, calls, hedge_after=hedge_after))


def test_call_that_answers_in_time_is_not_doubled(tmp_path):
    pipeline, stages = _pipeline(tmp_path)
    calls = Calls((0.0, "a"))
    assert _run(pipeline, calls) == "a"
    assert calls.started == 1 and stages == []


def test_slow_call_gets_a_backup_and_the_first_answer_wins(tmp_path):
    pipeline, stages = _pipeline(tmp_path)
    calls = Calls((5.0, "slow"), (0.0, "backup"))
    assert _run(pipeline, calls) == "backup"
    assert calls.started == 2 and calls.cancelled == 1, "늦은 쪽은 결과를 쓰기 전에 그만둔다"
    assert stages == ["tts:hedge"] and not pipeline._hedging

    late = Calls((0.1, "first"), (5.0, "backup"))
    assert _run(pipeline, late) == "first", "먼저 나간 호출이 먼저 오면 그것을 쓴다"
    assert late.started == 2 and late.cancelled == 1


def test_failed_call_is_retried_once_as_before(tmp_path):
    pipeline, stages = _pipeline(tmp_path)
    calls = Calls((0.0, RuntimeError("boom")), (0.0, "again"))
    assert _run(pipeline, calls) == "again"
    assert calls.started == 2 and stages == ["tts:error"], "실패는 곧바로 다시 부른다 — 겹쳐 보낸 것이 아니다"
    with pytest.raises(RuntimeError, match="two"):
        _run(pipeline, Calls((0.0, RuntimeError("one")), (0.0, RuntimeError("two"))))


def test_number_of_calls_stays_within_the_limit(tmp_path):
    pipeline, stages = _pipeline(tmp_path)
    calls = Calls((0.2, RuntimeError("slow and failed")), (0.3, RuntimeError("backup failed")), (0.0, "third"))
    with pytest.raises(RuntimeError, match="backup failed"):
        _run(pipeline, calls)
    assert calls.started == 2, "겹쳐 보냈으면 실패해도 더 부르지 않는다 (최초 1회 + 1회)"
    assert stages == ["tts:hedge", "tts:error", "tts:error"]

    waits = Calls((0.2, RuntimeError("slow and failed")), (0.4, "backup"))
    assert _run(pipeline, waits) == "backup", "먼저 나간 호출이 실패해도 예비 호출을 기다린다"


def test_hedging_can_be_turned_off(tmp_path):
    pipeline, stages = _pipeline(tmp_path, hedge=False)
    calls = Calls((0.2, "slow"), (0.0, "backup"))
    assert _run(pipeline, calls) == "slow"
    assert calls.started == 1 and stages == []


def test_only_one_backup_per_stage_at_a_time(tmp_path):
    """제공자가 통째로 느릴 때 호출이 두 배로 불어나지 않게 한다."""
    pipeline, stages = _pipeline(tmp_path)

    async def scenario():
        first, second = Calls((0.3, "a"), (0.3, "a2")), Calls((0.3, "b"), (0.0, "b2"))
        results = await asyncio.gather(pipeline._retry(JOB, "llm", first, hedge_after=0.05),
                                       pipeline._retry(JOB, "llm", second, hedge_after=0.1))
        return results, first.started, second.started

    results, first_started, second_started = asyncio.run(scenario())
    assert results == ["a", "b"] and (first_started, second_started) == (2, 1)
    assert stages == ["llm:hedge"] and not pipeline._hedging


def test_quota_error_stops_new_calls_but_waits_for_the_one_in_flight(tmp_path):
    pipeline, _ = _pipeline(tmp_path)
    calls = Calls((0.2, "first"), (0.0, TtsQuotaError("429", 60)))
    assert _run(pipeline, calls) == "first"
    with pytest.raises(TtsQuotaError):
        _run(pipeline, Calls((0.0, TtsQuotaError("429", 60)), (0.0, "never")))


def test_hedge_thresholds_sit_above_the_usual_latency():
    """서버 기록(2026-11-20): 이야기 멘트의 LLM 95% 7.3초, TTS 95% 8.9초, 문자 소개의 TTS 95% 14.2초."""
    assert HEDGE_LLM_SEC > 7.3
    assert tts_hedge_sec("가" * 80) > 8.9, "이야기 멘트(80자쯤)"
    assert tts_hedge_sec("가" * 250) > 14.2, "문자 소개(250자쯤)"
    assert tts_hedge_sec("") == 10.0 and tts_hedge_sec("가" * 1000) == 22.0
