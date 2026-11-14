"""방송에서 여러 번 쓴 표현을 다음 프롬프트에 알린다 — 직전 멘트 창 밖의 되풀이를 막는다 (2026-11-15 실제 방송)."""
import asyncio
from pathlib import Path

from onair_engine.domain import GenerationJob, Material, Prompt, Script, StationProfile
from onair_engine.pipeline.pipeline import GenerationPipeline
from onair_engine.pipeline.repeats import WornPhrases
from onair_engine.pipeline.safety import SafetyChecker
from onair_engine.pipeline.tts import make_tts
from onair_engine.prompt_data import WORN
from onair_engine.telemetry import Telemetry

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"
PROFILE = StationProfile(dj_name="새벽", tone="차분한 존댓말", concept="수험생 응원")


def test_phrase_used_in_two_scripts_is_worn():
    worn = WornPhrases()
    worn.add("수능이 나흘 남은 오늘, 다들 어떻게 보내고 계신가요.")
    assert worn.worn() == [], "한 번 쓴 말은 되풀이가 아니다"
    worn.add("자, 수능이 나흘 남은 오늘은 컨디션 얘기를 해 볼게요.")
    assert worn.worn() == ["수능이 나흘 남은"], "조사가 달라진 어절('오늘', '오늘은') 앞까지만 같은 묶음이다"
    worn.add("수능이 나흘 남은 오늘, 마지막 정리를 해 볼까요.")
    assert worn.worn() == ["수능이 나흘 남은"], "겹치는 묶음은 하나로 알린다 — 더 많이 쓴 쪽이다"


def test_same_phrase_twice_in_one_script_counts_once():
    worn = WornPhrases()
    worn.add("오늘도 힘내 봅시다 여러분. 정말 오늘도 힘내 봅시다 여러분.")
    assert worn.worn() == []


def test_material_in_the_prompt_is_not_a_habit():
    """곡 제목과 청취자 글은 말해야 하는 내용이다 — 두 번 나와도 되풀이로 세지 않는다."""
    worn = WornPhrases()
    material = "<data>곡: 밤이 깊었네 다시 한번 — 크라잉넛</data>"
    worn.add("다음 곡은 밤이 깊었네 다시 한번 입니다.", material)
    worn.add("방금 들으신 곡은 밤이 깊었네 다시 한번 이었어요.", material)
    assert worn.worn() == []


def test_most_used_phrases_come_first_and_the_list_is_capped():
    worn = WornPhrases(max_phrases=2)
    for _ in range(3):
        worn.add("따뜻한 차 한 잔 어떠세요")
    for i in range(2):
        worn.add(f"창밖을 한번 내다 보세요 {i}")
        worn.add(f"어깨를 한번 쭉 펴 보세요 {i}")
    first, second = worn.worn()
    assert "차 한 잔" in first and "한번" in second, "세 번 쓴 구절이 먼저다. 같은 구절의 묶음은 하나만 든다"
    worn.clear()
    assert worn.worn() == []


# ── 파이프라인 ────────────────────────────────────────────────────────

class Scripted:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.prompts: list[Prompt] = []

    async def generate(self, prompt: Prompt) -> Script:
        self.prompts.append(prompt)
        return Script(text=self.outputs.pop(0))


def _pipeline(tmp_path, llm) -> GenerationPipeline:
    return GenerationPipeline(
        station_id="st_test", profile=PROFILE, llm=llm, tts=make_tts("dummy"), safety=SafetyChecker(RULES),
        telemetry=Telemetry(tmp_path / "metrics.sqlite", "st_test"), audio_root=tmp_path / "audio",
        recent_segments=2)


def _job(n: int, *, plain: bool = False, step: str = "talk") -> GenerationJob:
    return GenerationJob(job_id=f"job_{n}", corner_type="topic_talk", step=step,
                         material=Material(topic="수능 D-4 화이팅"), plain=plain)


def test_pipeline_tells_the_llm_which_phrases_are_worn(tmp_path):
    llm = Scripted("수능이 나흘 남은 오늘, 긴장되시죠.", "다른 이야기를 잠깐 해 볼게요.", "창밖이 벌써 어둡네요.",
                   "수능이 나흘 남은 오늘은 잠 얘기를 해 볼게요.", "끝으로 한마디만 할게요.")
    pipeline = _pipeline(tmp_path, llm)

    async def run():
        for n in range(5):
            await pipeline.run(_job(n))

    asyncio.run(run())
    pipeline.telemetry.close()
    assert all(WORN not in p.system for p in llm.prompts[:4]), "두 번 나오기 전에는 알리지 않는다"
    last = llm.prompts[4].system
    assert WORN in last and "- 수능이 나흘 남은" in last
    assert "긴장되시죠" not in last, "직전 멘트 창(2개) 밖의 대본이다 — 창만으로는 이 되풀이를 못 본다"


def test_news_scripts_are_neither_counted_nor_told(tmp_path):
    line = "학교 공지에 따르면, 도서관이 열람실을 밤 열두 시까지 엽니다."
    llm = Scripted(line, line, line)
    pipeline = _pipeline(tmp_path, llm)

    async def run():
        for n in range(3):
            await pipeline.run(_job(n, plain=True, step="news_item"))

    asyncio.run(run())
    pipeline.telemetry.close()
    assert all(WORN not in p.system for p in llm.prompts), "뉴스는 출처 표현을 되풀이해야 한다"
    assert pipeline._worn.worn() == []
