"""뉴스 대본의 주관 표현 규칙 (CORNER_DESIGN 6.4절 U3, #286) — 규칙 목록, 다시 쓰기, 걸린 문장 빼기."""
import asyncio
import sqlite3
from pathlib import Path

import pytest

from onair_engine.domain import GenerationJob, Material, Prompt, Script, StationProfile
from onair_engine.pipeline.llm import LlmError
from onair_engine.pipeline.pipeline import GenerationPipeline
from onair_engine.pipeline.safety import SafetyChecker, split_sentences
from onair_engine.pipeline.tts import make_tts
from onair_engine.telemetry import Telemetry

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"
safety = SafetyChecker(RULES)
PROFILE = StationProfile(dj_name="해살", tone="밝은 존댓말", concept="아침 뉴스",
                         signature_phrases=["오늘도 출발해 볼까요"])
NEWS = {"title": "도서관 야간 개방 연장", "summary": "시험 기간 동안 열람실을 밤 열두 시까지 연다", "source": "학교 공지"}


# 2026-11-14에 실제 피드로 뽑은 대본의 문장이다 (docs/ENGINE_TUNING_LOG.md T-011)
@pytest.mark.parametrize(("sentence", "rule"), [
    ("다행히 다친 사람은 없었습니다.", "evaluation"),
    ("두 작품이 제목에 꿈과 환상을 내세운다는 점이 인상적이야.", "evaluation"),
    ("사고 뒤 달아난 일이 있었다는 게 참 무겁게 느껴져.", "evaluation"),
    ("보안이 얼마나 중요한지 다시 생각하게 돼.", "evaluation"),
    ("꾸준한 기반이 성과로 이어지는 모습이 눈에 남습니다.", "evaluation"),
    ("정상회담에서 어떤 이야기가 오갈지 지켜보겠습니다.", "watch"),
    ("실제 선임으로 이어질지 지켜봐야겠네.", "watch"),
    ("찬반 목소리가 맞붙은 만큼 지켜볼 소식이야.", "watch"),
    ("기묘한 방식으로 사랑을 좇는 이야기가 궁금해집니다.", "watch"),
    ("다친 분들이 잘 회복하시기를 바랍니다.", "wish"),
    ("가입자분들은 안내를 확인해 보시면 좋겠습니다.", "wish"),
    ("주말에 찾아보고 싶다!", "wish"),
    ("복구 상황을 차분히 확인하는 일이 중요하겠네요.", "judgment"),
    ("늘 주변을 살피셔야겠습니다.", "judgment"),
    ("앞으로도 눈여겨볼 만하겠습니다.", "watch"),
    ("스페인의 강세도 오래갈 것 같아.", "guess"),
    ("이런 상황에 대한 대응이 중요해 보입니다.", "judgment"),
    ("믿음을 쌓아 가는 시간이 필요해 보입니다.", "judgment"),
    ("과거를 잊지 않게 하는 힘이 되겠어.", "guess"),
    ("제 생각에는 조금 이른 결정입니다.", "first_person"),
    ("저는 이 소식이 반가웠습니다.", "evaluation"),
    ("여러분은 어떻게 생각하세요?", "address"),
    ("오랜 라이벌도 슬픔 앞에선 하나가 되는구나.", "reaction_ending"),
    ("장 마감 때는 분위기가 확 달라졌네.", "reaction_ending"),
    ("궁금해지는 소식이네요.", "watch"),
    ("이 논란은 회담에서 거론되지 않을 것으로 보입니다.", "forecast"),
    ("내년에도 오름세가 이어질 전망입니다.", "forecast"),
])
def test_subjective_sentences_are_caught(sentence, rule):
    hits = safety.check_news(sentence)
    assert hits and hits[0].rule == rule and hits[0].sentence == sentence, hits


@pytest.mark.parametrize("sentence", [
    # 사실만 전한 문장 — 걸리면 안 된다
    "영국 하원의원들은 반복적인 머리 충격과 신경퇴행성 질환의 연관성을 보여주는 증거가 늘고 있다며 법적 제한을 요구했습니다.",
    "지디넷코리아에 따르면, 줌라이언이 장비 등 예순 종 이상을 선보입니다.",
    "최 의원은 이재명 대통령의 재판과 윤석열 전 대통령의 재판이 같다고 말했습니다.",
    "분데스리가 구단이 김예건에게 관심을 보이고 있습니다.",
    "빠른 발과 드리블 능력으로 주목받고 있습니다.",
    "정부는 14일부터 4주간 석유 최고가격을 기존 수준으로 동결했어.",
    "다음 달부터 세 곳의 도서관이 무인 대출 로봇을 시범 운영하게 됩니다.",
    "당국은 상황을 지켜보고 있다고 밝혔습니다.",
    "코스피는 전날보다 오십이 포인트 내린 채 거래를 마쳤습니다.",
    # 남의 말을 옮긴 문장
    "유족은 안타깝다고 말했습니다.",
    "전문가들은 내년 전망이 우려스럽다며 대책을 요구했습니다.",
    "팬들은 아쉽다는 반응을 보였습니다.",
    "감독은 선수들이 대단하다고 평가했습니다.",
    # 따옴표 안 — 작품·프로그램 이름과 인용
    "MBC는 ‘놀라운 토요일’의 편성을 바꿨습니다.",
    "곡 제목은 ‘꿈꾸던 어른이 되었나요’입니다.",
    # 출처가 있는 전망
    "경향신문에 따르면, 대통령은 그 의혹을 거론하지 않을 것으로 예상됩니다.",
    "한국은행은 내년에 가격이 오를 것으로 보인다고 전망했습니다.",
    "경향신문은 삼성전자가 수혜를 볼 수 있다는 전망을 전했습니다.",
])
def test_factual_and_reported_sentences_pass(sentence):
    assert safety.check_news(sentence) == []


def test_reaction_endings_and_first_person_are_caught_even_in_reported_sentences():
    assert safety.check_news("그렇게 말했다고 하네요.")[0].rule == "reaction_ending"
    assert safety.check_news("저는 그 말이 맞다고 생각합니다.")[0].rule == "first_person"


def test_signature_phrase_is_not_used_in_news():
    text = "학교 공지에 따르면 열람실을 밤 열두 시까지 엽니다. 오늘도 출발해 볼까요."
    hits = safety.check_news(text, PROFILE.signature_phrases)
    assert [(h.rule, h.sentence) for h in hits] == [("signature", "오늘도 출발해 볼까요.")]
    assert safety.check_news(text) == [], "입버릇을 넘기지 않으면 보지 않는다"
    assert safety.check_l2(text) is None, "일반 출력단 규칙에는 걸리지 않는다"
    assert split_sentences(" 첫 문장입니다.  둘째 문장이에요! 셋째? ") == ["첫 문장입니다.", "둘째 문장이에요!", "셋째?"]


# ── 파이프라인 ────────────────────────────────────────────────────────

class Scripted:
    """정해 둔 대본을 차례로 돌려준다. 받은 프롬프트를 기록한다. LlmError를 넣으면 그 차례에 실패한다."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.prompts: list[Prompt] = []

    async def generate(self, prompt: Prompt) -> Script:
        self.prompts.append(prompt)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return Script(text=out)


CLEAN = "학교 공지에 따르면, 도서관이 시험 기간 동안 열람실을 밤 열두 시까지 엽니다. 기간은 이번 주부터입니다."
OPINION = "학교 공지에 따르면, 도서관이 시험 기간 동안 열람실을 밤 열두 시까지 엽니다. 수험생에게 반가운 소식이네요."


def _run(tmp_path, llm, step="news_item"):
    pipeline = GenerationPipeline(
        station_id="st_test", profile=PROFILE, llm=llm, tts=make_tts("dummy"), safety=SafetyChecker(RULES),
        telemetry=Telemetry(tmp_path / "metrics.sqlite", "st_test"), audio_root=tmp_path / "audio")
    job = GenerationJob(job_id="job_n", corner_type="section", step=step,
                        material=Material(items=[NEWS], label="종합"), plain=True)
    sub = asyncio.run(pipeline.run(job))
    script = pipeline._pending_air.get(sub.id) if sub is not None else None
    pipeline.telemetry.close()
    db = sqlite3.connect(tmp_path / "metrics.sqlite")
    try:
        stages = db.execute("SELECT stage, result FROM generation_log ORDER BY rowid").fetchall()
        caught = db.execute("SELECT rule, matched, sentence FROM subjective_log ORDER BY rowid").fetchall()
    finally:
        db.close()
    return script, stages, caught


def test_clean_news_goes_out_as_written(tmp_path):
    llm = Scripted(CLEAN)
    script, stages, caught = _run(tmp_path, llm)
    assert script == CLEAN and len(llm.prompts) == 1 and caught == []
    assert "subjective" not in [stage for stage, _ in stages], "걸리지 않으면 기록도 남기지 않는다"


def test_subjective_news_is_rewritten_once(tmp_path):
    llm = Scripted(OPINION, CLEAN)
    script, stages, caught = _run(tmp_path, llm)
    assert script == CLEAN and len(llm.prompts) == 2
    assert "반가운" in llm.prompts[1].user and "사실만으로 다시 쓴다" in llm.prompts[1].user
    assert llm.prompts[1].user.startswith(llm.prompts[0].user), "같은 소재로 다시 쓴다"
    assert ("subjective", "rewritten") in stages
    assert [(rule, sentence) for rule, _, sentence in caught] == [("evaluation", "수험생에게 반가운 소식이네요.")]


def test_still_subjective_news_loses_only_the_offending_sentences(tmp_path):
    llm = Scripted(OPINION, OPINION.replace("반가운 소식이네요", "도움이 되면 좋겠습니다"))
    script, stages, caught = _run(tmp_path, llm)
    assert script == "학교 공지에 따르면, 도서관이 시험 기간 동안 열람실을 밤 열두 시까지 엽니다."
    assert ("subjective", "trimmed") in stages and len(llm.prompts) == 2, "다시 쓰기는 한 번만 한다"
    assert [rule for rule, _, _ in caught] == ["evaluation", "wish"]


def test_news_with_nothing_factual_left_is_not_aired(tmp_path):
    llm = Scripted("참 반가운 소식이네요.", "정말 다행입니다. 앞으로가 기대됩니다.")
    script, stages, _ = _run(tmp_path, llm)
    assert script is None and ("subjective", "reject") in stages


def test_failed_rewrite_falls_back_to_trimming_the_first_script(tmp_path):
    llm = Scripted(OPINION, LlmError("openai 503"))
    script, stages, _ = _run(tmp_path, llm)
    assert script == "학교 공지에 따르면, 도서관이 시험 기간 동안 열람실을 밤 열두 시까지 엽니다."
    assert ("subjective", "trimmed") in stages


def test_rule_applies_to_news_content_only(tmp_path):
    greeting = "좋은 아침입니다. 오늘도 좋은 하루 보내시길 바랍니다. 오늘도 출발해 볼까요."
    llm = Scripted(greeting)
    script, _, caught = _run(tmp_path, llm, step="opening_greeting")
    assert script == greeting and len(llm.prompts) == 1 and caught == [], "뉴스 형식의 인사에는 걸지 않는다"
