"""방송 중 페르소나 전환 (#261) — station.persona_changed로 대본·목소리·연결 멘트를 바꾸고, 걸리면 이전 페르소나로 계속한다."""
import asyncio
from pathlib import Path

from onair_engine.domain import GenerationJob, Material, StationProfile
from onair_engine.engine import EngineSettings
from onair_engine.manager import EngineManager
from onair_engine.pipeline.llm import DummyLlmClient
from onair_engine.pipeline.pipeline import GenerationPipeline
from onair_engine.pipeline.safety import SafetyChecker
from onair_engine.pipeline.tts import DummyTtsClient
from onair_engine.telemetry import Telemetry
from test_corner_skip import Gated, _gated_engine, _summaries, _until
from test_scheduler import _engine

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"
PERSONA = {
    "persona_id": "psn_beongae",
    "style": {"formality": "casual", "energy": "high", "humor": "often"},
    "voice": "voice-ko-2",
    "dj_name": "번개",
    "concept": "불금 하이텐션 수다 라디오",
    "tone": "친구 같은 반말. 텐션이 아주 높다",
    "examples": ["와 다들 모였어? 바로 달려 보자!", "이거 실화야?", "채팅창 불났다!"],
    "forbidden": ["존댓말로 말하기"],
}


class Switchable(Gated):
    """페르소나 전환을 받는 가짜 파이프라인 — 바뀐 프로필과, 작업마다 그때의 DJ를 적는다."""

    def __init__(self, block):
        super().__init__(block)
        self.dj = "테스트"
        self.made_by = {}

    def switch_persona(self, profile, tts):
        self.dj = profile.dj_name

    async def run(self, job):
        sub = await super().run(job)
        self.made_by[sub.id] = self.dj
        return sub


def _switchable_engine(tmp_path, block, **config):
    engine, transport, _ = _gated_engine(tmp_path, block, **config)
    engine.scheduler.pipeline = pipeline = Switchable(block)
    return engine, transport, pipeline


def test_switch_drops_unsubmitted_work_and_the_new_dj_quotes_the_returned_chat(tmp_path):
    engine, transport, pipeline = _switchable_engine(
        tmp_path, lambda job: bool(job.material.quotes), corners=["topic_talk"])
    scheduler = engine.scheduler

    async def scenario():
        await engine.submit_request("chat", "오늘 너무 피곤해요", "phone", request_id="chat")
        run = asyncio.create_task(engine.run())
        await _until(lambda: pipeline.waiting == 1, "채팅을 인용하는 멘트 생성")
        first = pipeline.jobs[-1]
        assert await engine.on_persona_changed({"persona": PERSONA})
        await _until(lambda: pipeline.waiting == 2, "새 DJ가 되돌아온 채팅을 다시 인용")
        assert pipeline.jobs[-1] is not first and pipeline.jobs[-1].corner_type == first.corner_type
        assert scheduler.inflight == 1, "버린 생성은 동시 생성 수에 넣지 않는다"
        pipeline.open.set()
        await _until(lambda: any(s.request_ref == "chat" for s in transport.segments), "인용 멘트 제출")
        engine.stop()
        await run

    asyncio.run(scenario())
    config = engine.config
    assert (config.profile.dj_name, config.persona_id, config.voice) == ("번개", "psn_beongae", "voice-ko-2")
    quoting = [s for s in transport.segments if s.request_ref == "chat"]
    assert len(quoting) == 1, "이전 DJ가 만들던 멘트는 나가지 않고 채팅은 한 번만 인용된다"
    assert pipeline.made_by[quoting[0].id] == "번개"
    states = [str(state) for rid, state in transport.states if rid == "chat"]
    assert states[-4:] == ["generating", "queued", "generating", "generated"]
    assert scheduler._failures.get("chat", 0) == 0, "페르소나 전환은 입력의 실패로 세지 않는다"
    summaries = _summaries(tmp_path)
    assert "페르소나 전환 · 테스트 → 번개 · 만들던 멘트 1개 버림" in summaries
    assert engine.topic == "테스트 방송", "주제는 페르소나를 바꿔도 그대로다"


def test_the_new_dj_restarts_the_talk_corner(tmp_path):
    engine, transport, pipeline = _switchable_engine(
        tmp_path, lambda job: job.corner_type == "topic_talk", corners=["topic_talk"])

    async def scenario():
        run = asyncio.create_task(engine.run())
        await _until(lambda: pipeline.waiting == 1, "주제 토크 첫 멘트 생성")
        first = pipeline.jobs[-1]
        assert await engine.on_persona_changed({"persona": PERSONA})
        await _until(lambda: pipeline.waiting == 2, "새 DJ의 주제 토크 첫 멘트 생성")
        again = pipeline.jobs[-1]
        engine.stop()
        pipeline.open.set()
        await run
        return first, again

    first, again = asyncio.run(scenario())
    assert (first.step, again.corner_type, again.step) == ("talk_open", "topic_talk", "talk_resume")
    assert not any(s.id == f"talk_open:{pipeline.jobs.index(first) + 1}" for s in transport.segments)
    summaries = _summaries(tmp_path)
    at = summaries.index("페르소나 전환 · 테스트 → 번개 · 만들던 멘트 1개 버림")
    assert "코너 종료 · 오늘의 이야기" in summaries[at - 2:at], "이전 DJ의 코너를 끝낸다"
    assert "코너 진입 · 오늘의 이야기" in summaries[at:], "하던 말 코너를 새 DJ가 다시 한다"


def test_switch_during_a_song_intro_lets_the_new_dj_talk_first(tmp_path):
    engine, transport, pipeline = _switchable_engine(
        tmp_path, lambda job: job.step == "music_intro", corners=["topic_talk"])

    async def scenario():
        run = asyncio.create_task(engine.run())
        await _until(lambda: pipeline.waiting == 1, "곡 소개 생성")
        intro = pipeline.jobs[-1]
        assert await engine.on_persona_changed({"persona": PERSONA})
        await _until(lambda: len(pipeline.jobs) > pipeline.jobs.index(intro) + 1, "새 DJ의 첫 멘트")
        after = pipeline.jobs[pipeline.jobs.index(intro) + 1]
        engine.stop()
        pipeline.open.set()
        await run
        return intro, after

    intro, after = asyncio.run(scenario())
    assert after.step != "music_intro", "곡 소개를 다시 하지 않고 다음 코너로 간다"
    later = pipeline.jobs[pipeline.jobs.index(intro) + 1:]
    assert (engine.tracks.peek() == intro.material.track
            or any(job.material.track == intro.material.track for job in later)), "소개하려던 곡은 다음 곡 차례로 돌아간다"
    dropped = f"music_intro:{pipeline.jobs.index(intro) + 1}"
    assert not any(s.id == dropped for s in transport.segments), "이전 DJ가 만들던 곡 소개는 나가지 않는다"


def test_rejected_persona_keeps_the_old_one(tmp_path):
    engine, _, _ = _switchable_engine(tmp_path, lambda job: False)

    async def known(voice):
        return voice == "voice-ko-1"

    async def scenario():
        results = [
            await engine.on_persona_changed({"persona": {**PERSONA, "examples": ["하나"]}}),
            await engine.on_persona_changed({}),
            await engine.on_persona_changed({"persona": {k: v for k, v in PERSONA.items() if k != "persona_id"}}),
            await engine.on_persona_changed({"persona": {**PERSONA, "tone": "example.com이에요"}}),
            await engine.on_persona_changed({"persona": {**PERSONA, "dj_name": "example.com"}}),
            await engine.on_persona_changed({"persona": PERSONA}, voice_known=known),
        ]
        assert results == [False] * 6
        assert engine._glue_task is None, "거절한 페르소나의 연결 멘트는 합성하지 않는다"

    asyncio.run(scenario())
    engine.telemetry.close()
    assert engine.config.profile.dj_name == "테스트" and engine.scheduler._persona is None
    rejected = [s for s in _summaries(tmp_path) if s.startswith("페르소나 전환 거절 · ")]
    assert len(rejected) == 6
    assert "persona.examples" in rejected[0] and "persona_id" in rejected[2]
    assert "persona.tone" in rejected[3] and "persona.dj_name" in rejected[4]
    assert "voice-ko-2" in rejected[5] and "보이스 목록에 없음" in rejected[5]


def test_redelivered_persona_event_is_ignored(tmp_path):
    engine, _, _ = _switchable_engine(tmp_path, lambda job: False)

    async def scenario():
        assert await engine.on_persona_changed({"persona": PERSONA})
        pending = engine.scheduler._persona
        assert not await engine.on_persona_changed({"persona": PERSONA}), "바꾸는 중인 페르소나"
        assert engine.scheduler._persona is pending
        await engine.scheduler._apply_persona()
        assert not await engine.on_persona_changed({"persona": PERSONA}), "이미 바꾼 페르소나"
        assert engine.scheduler._persona is None
        await engine._glue_task

    asyncio.run(scenario())
    engine.telemetry.close()
    assert not [s for s in _summaries(tmp_path) if "거절" in s]


def test_glue_is_rendered_again_in_a_new_folder_and_used_after_the_switch(tmp_path):
    engine, transport = _engine(tmp_path, corners=["topic_talk"], broadcast_minutes=1)

    async def scenario():
        await engine.glue.prerender()
        old = engine.glue
        assert await engine.on_persona_changed({"persona": PERSONA})
        assert engine.scheduler.glue is old, "편성 루프가 바꾸기 전까지는 이전 연결 멘트를 쓴다"
        await engine._glue_task
        await engine.scheduler._apply_persona()
        return old

    old = asyncio.run(scenario())
    engine.telemetry.close()
    new = engine.scheduler.glue
    assert new is not old and len(new) == len(old) > 0
    old_ref, new_ref = old.get("topic_talk", "call")[0], new.get("topic_talk", "call")[0]
    assert "/glue/" in old_ref and "/glue_1/" in new_ref
    audio = tmp_path / "audio"
    assert (audio / old_ref).exists() and (audio / new_ref).exists(), "이전 문구 파일은 재생 전일 수 있어 지우지 않는다"
    assert any(s.startswith(f"연결 멘트 {len(new)}개 다시 합성") for s in _summaries(tmp_path))
    assert engine.scheduler.pipeline.profile.dj_name == "번개"
    assert engine.scheduler.pipeline.tts is not None and not transport.segments


def test_pipeline_forgets_the_previous_djs_lines(tmp_path):
    class Held(DummyLlmClient):
        """generate가 문이 열릴 때까지 끝나지 않는다 — 대본을 쓰는 사이 페르소나가 바뀌는 상황."""

        def __init__(self):
            super().__init__(delay_sec=0)
            self.gate = asyncio.Event()
            self.gate.set()

        async def generate(self, prompt):
            script = await super().generate(prompt)
            await self.gate.wait()
            return script

    llm = Held()
    old = StationProfile(dj_name="고요", tone="차분한 존댓말", concept="테스트 방송")
    new = StationProfile(dj_name="번개", tone="반말", concept="테스트 방송")
    pipeline = GenerationPipeline(
        station_id="st_test", profile=old, llm=llm, tts=DummyTtsClient(), safety=SafetyChecker(RULES),
        telemetry=Telemetry(tmp_path / "metrics.sqlite", "st_test"), audio_root=tmp_path / "audio")

    def job():
        return GenerationJob(job_id="job", corner_type="topic_talk", step="talk_open",
                             material=Material(topic="겨울 간식"))

    async def scenario():
        assert await pipeline.run(job()) is not None
        assert len(pipeline._recent) == 1
        llm.gate.clear()
        writing = asyncio.create_task(pipeline.run(job()))  # 이전 DJ의 프롬프트로 쓰기 시작한다
        await asyncio.sleep(0.05)
        tts = DummyTtsClient()
        pipeline.switch_persona(new, tts)
        assert not pipeline._recent and pipeline.profile is new and pipeline.tts is tts
        llm.gate.set()
        await writing
        assert not pipeline._recent, "전환 전에 쓰기 시작한 대본은 새 DJ의 맥락에 넣지 않는다"
        assert await pipeline.run(job()) is not None
        assert len(pipeline._recent) == 1

    asyncio.run(scenario())
    pipeline.telemetry.close()


def test_manager_routes_persona_changed_and_checks_the_voice_list(tmp_path):
    engine, _, _ = _switchable_engine(tmp_path, lambda job: False)
    manager = EngineManager(EngineSettings(), voice_catalog=lambda: ["voice-ko-1", "voice-ko-2"])

    async def scenario():
        event = {"type": "station.persona_changed", "payload": {"persona": {**PERSONA, "voice": "voice-en-9"}}}
        await manager._route_station_event(engine, event)
        assert engine.scheduler._persona is None
        await manager._route_station_event(
            engine, {"type": "station.persona_changed", "payload": {"persona": PERSONA}})
        assert engine.scheduler._persona.profile.dj_name == "번개"
        await engine._glue_task

    asyncio.run(scenario())
    engine.telemetry.close()
    assert any("voice-en-9" in s for s in _summaries(tmp_path))
