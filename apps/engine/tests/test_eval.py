"""대본 품질 평가 — 자동 지표와 러너 (실제 API 호출 없음)."""
import asyncio
from pathlib import Path

from onair_engine.catalog import Track
from onair_engine.domain import StationProfile
from onair_engine.eval import checks
from onair_engine.eval.runner import (
    RecordingLlm,
    Scenario,
    parse_judge,
    render_quotes,
    run_scenario,
    summarize,
)
from onair_engine.pipeline.llm import DummyLlmClient
from onair_engine.sources.rss import FeedItem

RULES = Path(__file__).resolve().parent.parent / "config" / "safety_rules.yaml"
PROFILE = StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송")


def test_speech_level():
    assert checks.speech_level("오늘도 고마워요.") == "polite"
    assert checks.speech_level("다음 곡 들려드리겠습니다!") == "polite"
    assert checks.speech_level("그렇죠?") == "polite"
    assert checks.speech_level("이 노래 진짜 좋아.") == "casual"
    assert checks.speech_level("일단 이 곡만 듣고 하자.") == "casual"
    assert checks.speech_level("주말 오후 수다 라디오.") is None  # 명사로 끝나면 판단하지 않는다


def test_expected_level_reads_tone():
    assert checks.expected_level("차분하고 따뜻한 존댓말") == "polite"
    assert checks.expected_level("친구 같은 반말. 존댓말을 섞지 않는다") == "casual"
    assert checks.expected_level("차분하게") is None


def test_level_violations_skip_titles_from_source():
    text = "다음 곡이에요. 아이유, 밤편지. 이거 진짜 좋아."
    assert checks.level_violations(text, "polite", source="곡: 밤편지 — 아이유") == ["이거 진짜 좋아."]
    assert checks.level_violations(text, None) == []


def test_text_metrics():
    assert checks.est_seconds("가나다라마바 사아자차카타") == 2.0
    assert checks.sentence_limit("곡 사이를 잇는 멘트를 1~2문장으로 작성하라.") == 2
    assert checks.sentence_limit("오프닝을 작성하라.") is None
    assert set(checks.leaked_markup("**안녕하세요** 🎵")) == {"*", "🎵"}
    assert checks.found("오늘도 힘 내세요", ["힘내세요"]) == ["힘내세요"]
    assert checks.similarity("오늘 밤도 함께해요", "오늘 밤도 함께해요") == 1.0
    assert checks.similarity("오늘 밤도 함께해요", "비 소식 있어요") < 0.2
    assert checks.opener("안녕하세요, 새벽입니다. 반가워요.") == "안녕하세요, 새벽입니다"


def test_parse_judge():
    j = parse_judge("캐릭터 4 자연스러움 3 과제 5 근거: 말투는 맞지만 문장이 길다")
    assert (j["character"], j["natural"], j["task"]) == (4, 3, 5)
    assert parse_judge("평가할 수 없습니다") is None


def test_run_scenario_with_dummy(tmp_path):
    scenario = Scenario(
        sequence=["opening_greeting", "music_intro", "story", "talk+chat", "news_item"],
        tracks=[Track("t1", "밤편지", "아이유", "calm", 1000)],
        news=[FeedItem("첫 서리", "영하권", "https://www.kma.go.kr/1")],
        stories=[{"id": "ok", "body": "시험 공부 중이에요", "expect": "air"},
                 {"id": "bad", "body": "팀장 박철수 망신 줘요", "expect": "reject",
                  "must_not_contain": ["박철수"]}],
    )
    llm = RecordingLlm(DummyLlmClient(delay_sec=0, seed=0))
    records = asyncio.run(run_scenario(PROFILE, scenario, llm, rules=RULES, recent_segments=4,
                                       workdir=tmp_path, run=0))
    assert [r["corner"] for r in records] == scenario.sequence
    assert all(r["outcome"] == "aired" for r in records)
    # 더미는 사연·채팅을 그대로 읽는다 — 거부해야 할 사연이 송출되면 경고가 붙어야 한다
    bad = next(r for r in records if r["case"] == "bad")
    assert any(f.startswith("거부 누락") for f in bad["flags"])
    assert any(f.startswith("금지 문자열 노출") for f in bad["flags"])
    summary = summarize(records)
    assert summary["송출"] == 5 and summary["판정 오류"] == 1


def test_scenario_file_steps_are_known():
    scenario = Scenario.load(Path(__file__).resolve().parent.parent / "config" / "eval_cases.yaml")
    assert "talk+chat" in scenario.sequence and scenario.topic


def test_chat_cases_record_quote_check(tmp_path):
    # chats가 있으면 +chat 단계는 사연이 아니라 짧은 채팅을 꺼낸다 — 곡 소개에도 붙일 수 있다 (#258)
    scenario = Scenario(
        sequence=["talk+chat", "music_intro+chat", "story"],
        tracks=[Track("t1", "밤편지", "아이유", "calm", 1000)], news=[],
        stories=[{"id": "s1", "body": "시험 공부 중이에요", "expect": "air"}],
        chats=[{"id": "c1", "type": "on_topic", "body": "저는 김치찌개 끓였어요"},
               {"id": "c2", "type": "dj_question", "body": "DJ님은 뭐 드셨어요?"}],
    )
    llm = RecordingLlm(DummyLlmClient(delay_sec=0, seed=0))
    records = asyncio.run(run_scenario(PROFILE, scenario, llm, rules=RULES, recent_segments=4,
                                       workdir=tmp_path, run=0))
    talk, intro, story = records
    assert (talk["chat"], talk["chat_type"], talk["quote"]) == ("저는 김치찌개 끓였어요", "on_topic", "ok")
    assert intro["corner"] == "music_intro+chat" and intro["chat_type"] == "dj_question"
    assert talk["quote_match"] == 1.0, "더미는 채팅을 그대로 인용한다"
    assert story["case"] == "s1" and "quote" not in story, "채팅을 넣지 않은 멘트는 검사하지 않는다"
    assert summarize(records)["인용 누락"] == "0/2"

    intro.update(quote="missing", quote_match=0.2)  # 실제 LLM이 풀어 말한 경우
    text = "\n".join(render_quotes({"p": records}))
    assert "| on_topic | 0/1 | - | 0/1 |" in text and "| dj_question | - | 1/1 | 1/1 |" in text
    assert "| 합계 | 0/1 | 1/1 | 1/2 |" in text
    assert "DJ님은 뭐 드셨어요?" in text and intro["text"] in text
    assert render_quotes({"p": [story]}) == []


def test_chat_case_file_covers_both_steps():
    scenario = Scenario.load(Path(__file__).resolve().parent.parent / "config" / "eval_cases_chat.yaml")
    chat_steps = [s for s in scenario.sequence if s.endswith("+chat")]
    assert len(chat_steps) == len(scenario.chats), "채팅이 남거나 모자라지 않는다"
    by_step: dict[str, set] = {}
    for step, chat in zip(chat_steps, scenario.chats, strict=True):
        by_step.setdefault(step, set()).add(chat["type"])
    assert by_step["talk+chat"] == by_step["music_intro+chat"], "유형마다 두 단계에 모두 들어간다"
    assert sum(s.startswith("music_intro") for s in chat_steps) <= len(scenario.tracks)
