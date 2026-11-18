"""심사 AI — 기준표, 기록 읽기, 코드로 재는 항목, 심사 AI의 답 확인, 채점, 결함 주입 (실제 API 호출 없음)."""
import asyncio
import json
import random
from pathlib import Path

import pytest

from onair_engine.eval import judge as J
from onair_engine.eval import judge_defects as D
from onair_engine.eval.judge_rules import CHECKS, load_broadcast
from onair_engine.pipeline.llm import LlmError

RUBRIC = J.load_rubric()
PERSONA = "config/personas/saebyeok.yaml"


def _speech(at, script, corner="topic_talk", reflected=(), sec=8.0):
    return {"at": at, "kind": "speech", "sec": sec, "segment_id": f"seg{at}", "corner_type": corner,
            "corner": {"opening": "오프닝", "closing": "엔딩", "music_intro": "곡 소개"}.get(corner, "오늘의 이야기"),
            "script": script, "text": None, "reflected": list(reflected)}


def _chat(at, rid, who, body, kind="채팅"):
    return {"at": at, "kind": "input", "sec": 0, "segment_id": None, "corner_type": None, "corner": None,
            "script": None, "text": f"{kind} {rid} ({who}): {body}", "reflected": []}


def _song(at, title="첫눈 오는 밤 - 가상의 가수"):
    return {"at": at, "kind": "music", "sec": 120.0, "segment_id": None, "corner_type": None, "corner": None,
            "script": None, "text": title, "reflected": []}


PLAYED = [
    _speech(0, "심야 스터디 라디오, 지금 시작합니다. 천천히 가요.", "opening"),
    _speech(10, "오늘은 겨울 간식 이야기를 나눠 볼게요.", "opening"),
    _speech(20, None),  # 고정 문구
    _speech(24, "군고구마는 껍질을 벗길 때 김이 올라오죠. 손끝부터 따뜻해져요."),
    _chat(30, "r1", "question1·호기심 질문형", "새벽님은 팥호빵파예요 야채호빵파예요?"),
    _speech(34, "호빵은 반으로 나눠 먹으면 더 맛있어요. 여러분은 어떤 호빵을 좋아하세요?"),
    _chat(40, "r2", "active1·활발한 소통형", "군고구마 얘기 들으니까 배고파요 ㅠㅠ"),
    _speech(44, "팥호빵파냐고 물어보신 분, 저는 팥호빵 쪽이에요.", reflected=["r1"]),
    _speech(54, "이어서 들려드릴 곡은 첫눈 오는 밤입니다.", "music_intro"),
    _song(62),
    _speech(182, "곡 잘 들으셨나요. 붕어빵 이야기를 이어가 볼게요."),
    _chat(190, "r3", "active1·활발한 소통형", "노래 좋네요"),
    _speech(194, "배고프다고 하신 분, 저도 군고구마가 생각나네요.", reflected=["r2"]),
    _speech(204, "붕어빵은 꼬리부터 드시는 분이 많죠."),
    _speech(214, "오늘은 겨울 간식 이야기를 나눴어요. 함께해 주셔서 고맙습니다.", "closing"),
]
AUDIENCE = [{"id": "question1", "sent": [{"request_id": "r1", "after": "군고구마는 껍질을 벗길 때 김이 올라오죠. 손끝부터 따뜻해져요."}]},
            {"id": "active1", "sent": [{"request_id": "r3", "after": "(곡) 첫눈 오는 밤 - 가상의 가수"}]}]


def _run_dir(tmp_path: Path, played=None, *, name="run-1", sim=None, fallback=0, persona=PERSONA) -> Path:
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "played.json").write_text(json.dumps({"played": played or PLAYED}, ensure_ascii=False), encoding="utf-8")
    (folder / "run.json").write_text(json.dumps({
        "run": 1, "persona": persona, "minutes": 20,
        "scenario": {"name": "aud_3", "station": {"topic": "겨울 간식", "music_taste": []},
                     "tracks": [{"title": "첫눈 오는 밤", "artist": "가상의 가수"}]}}, ensure_ascii=False), encoding="utf-8")
    (folder / "report.json").write_text(json.dumps({
        "topic": "겨울 간식", "started_at": 1795027254.0,  # 2026-11-19 03:40 한국 시간
        "generation": {"outcomes": {"fallback": fallback}},
        "sim": {"silence_max_sec": 0.0, "audience": AUDIENCE, **(sim or {})}}, ensure_ascii=False), encoding="utf-8")
    return folder


class FakeLlm:
    """항목 열쇠가 든 프롬프트에 정해 둔 답을 준다. 받은 프롬프트를 기록한다."""

    def __init__(self, answers: dict | None = None, fail: tuple[str, ...] = (), garbage_first: bool = False):
        self.answers = answers or {}
        self.fail = fail
        self.garbage_first = garbage_first
        self.prompts: list[str] = []
        self.usage = {"in": 1000, "out": 500}

    async def complete(self, prompt, *, max_tokens):
        self.prompts.append(prompt.user)
        if "후보" in prompt.user and '"pick"' in prompt.user:
            if "blind" in self.fail:
                raise LlmError("503")
            label = next(line[1] for line in prompt.user.splitlines()
                         if line.startswith("[") and line.endswith("]") and len(line) == 3
                         and self._is(prompt.user, line[1], self.answers.get("pick", "천천히 가요")))
            return json.dumps({"pick": label, "why": "문장이 짧고 차분하다"}, ensure_ascii=False)
        keys = [item.key for _, item in RUBRIC.pairs() if f"[{item.key}]" in prompt.user]
        if any(k in self.fail for k in keys):
            raise LlmError("503")
        if self.garbage_first and len(self.prompts) == 1:
            return "죄송하지만 JSON으로 답하기 어렵습니다"
        body = {k: self.answers.get(k, {"na": False, "findings": []}) for k in keys}
        return "```json\n" + json.dumps(body, ensure_ascii=False) + "\n```"

    @staticmethod
    def _is(user: str, label: str, signature: str) -> bool:
        block = user.split(f"[{label}]\n", 1)[1].split("\n\n", 1)[0]
        return f"입버릇: {signature}" in block


def _judge(tmp_path, llm, **kwargs) -> dict:
    return asyncio.run(J.Judge(RUBRIC, llm).judge(load_broadcast(_run_dir(tmp_path, **kwargs))))


# ── 기준표 ────────────────────────────────────────────────────────────

def test_rubric_has_nine_criteria_36_items_and_the_agreed_weights():
    weights = {c.name: c.weight for c in RUBRIC.criteria}
    assert len(weights) == 9 and sum(weights.values()) == 100 and len(RUBRIC.pairs()) == 36
    heavy = {"방송 흐름 맥락 유지", "청취자 소통 및 공감력", "방송 진행의 완성도", "콘텐츠 전달력"}
    assert {name for name, w in weights.items() if w == 15} == heavy and all(w == 8 for n, w in weights.items() if n not in heavy)
    for _, item in RUBRIC.pairs():
        assert (item.by == "judge") == (item.check is None), f"{item.key}: 코드로 재는 항목만 check가 있다"
    calls = RUBRIC.calls()
    assert [i.key for i in calls["language"]] == ["G1", "G2", "H2"] and [i.key for i in calls["blind"]] == ["E2"]
    assert "A2" in [i.key for i in calls["A"]] and "I5" not in [i.key for i in calls["I"]], "코드로만 재는 항목은 묻지 않는다"
    assert set(RUBRIC.calls({"B"})) == {"B", "listeners"} and [i.key for i in RUBRIC.calls({"I4"})["I"]] == ["I4"]


def test_item_rejects_unknown_check_and_judge_item_without_definition():
    with pytest.raises(ValueError, match="모르는 검사"):
        J.Item(key="X1", name="x", by="rule", unit="ment", check="nope")
    with pytest.raises(ValueError, match="problem"):
        J.Item(key="X2", name="x", by="judge", unit="ment")
    assert J.Item(key="X3", name="x", by="judge", unit="ment", problem="p", allow=1, allow_rate=0.1).allowed(40) == 4


# ── 기록 읽기 ──────────────────────────────────────────────────────────

def test_broadcast_numbers_ments_chats_songs_and_hides_listener_types(tmp_path):
    b = load_broadcast(_run_dir(tmp_path))
    assert [m.id for m in b.ments][:3] == ["M1", "M2", "M3"] and b.ments[2].script is None and len(b.scripted) == 10
    assert [(c.id, c.who) for c in b.chats] == [("C1", "L1"), ("C2", "L2"), ("C3", "L2")]
    assert b.ment("M6").takes == ["C1"] and [m.id for m in b.taken_by("C2")] == ["M9"]
    assert b.chat("C1").after == "M4" and b.chat("C3").after == "S1" and b.chat("C2").after is None
    assert b.ment("M8").after_song and not b.ment("M9").after_song and b.has_ending
    assert (b.units("ment"), b.units("chat"), b.units("listener"), b.units("broadcast")) == (10, 3, 2, 1)
    record = J.render_record(b)
    assert "[C1 0:30 채팅 L1 · 쓸 때 듣던 것 M4] 새벽님은 팥호빵파예요" in record and "호기심 질문형" not in record
    assert "[M6 0:44 오늘의 이야기 · C1 받음]" in record and "[M8 3:02 오늘의 이야기 · 곡 뒤 첫 멘트]" in record
    assert "[M3 0:20 오늘의 이야기] (고정 문구 — 대본 기록 없음)" in record and "[S1 1:02 곡 2:00] 첫눈 오는 밤 - 가상의 가수" in record
    info = J.station_info(b)
    assert "목요일 새벽 3시 40분" in info and "오늘의 주제: 겨울 간식" in info and "중간에서 끝났다" not in info
    assert "받은 멘트: 없음" in J.render_listeners(b) and "[L2] 보낸 글 2건" in J.render_listeners(b)


def test_record_without_ending_says_it_was_cut_short(tmp_path):
    b = load_broadcast(_run_dir(tmp_path, PLAYED[:-1]))
    assert not b.has_ending and "방송 중간에서 끝났다" in J.station_info(b)


# ── 코드로 재는 항목 ──────────────────────────────────────────────────

def _check(tmp_path, name, played, **kwargs) -> list:
    return CHECKS[name](load_broadcast(_run_dir(tmp_path, played, **kwargs)))


def test_clean_record_passes_every_code_check(tmp_path):
    b = load_broadcast(_run_dir(tmp_path))
    assert {name: [f.why for f in check(b)] for name, check in CHECKS.items() if check(b)} == {}


def test_restart_flags_a_greeting_outside_the_opening(tmp_path):
    played = [*PLAYED[:6], _speech(41, "안녕하세요, 심야 스터디 라디오의 새벽입니다."), *PLAYED[6:]]
    assert [f.at for f in _check(tmp_path, "restart", played)] == ["M6"]


def test_repeated_quote_and_one_listener(tmp_path):
    played = [*PLAYED[:13], _speech(205, "배고프다는 분 또 생각나네요.", reflected=["r2"]),
              _speech(210, "아까 배고프다고 하신 분.", reflected=["r2"]), *PLAYED[13:]]
    found = _check(tmp_path, "repeated_quote", played)
    assert [f.at for f in found] == ["C2"] and "멘트 3개(M9, M10, M11)" in found[0].why
    crowd = [_speech(0, "시작합니다.", "opening"), _chat(1, "a1", "a", "하나"), _chat(2, "b1", "b", "둘"), _chat(3, "c1", "c", "셋"),
             _chat(4, "b2", "b", "넷"), _chat(5, "c2", "c", "다섯"),
             *[_speech(10 + n, f"첫 분 이야기 {n}", reflected=["a1"]) for n in range(4)], _speech(20, "둘째 분.", reflected=["b1"])]
    assert "5건 가운데 4건이 L1" in _check(tmp_path, "one_listener", crowd)[0].why
    assert _check(tmp_path, "one_listener", PLAYED) == [], "받은 글이 적으면 쏠림을 말할 수 없다"


def test_breaks_counts_long_silence_and_fallbacks(tmp_path):
    found = _check(tmp_path, "breaks", PLAYED, sim={"silence_max_sec": 7.5}, fallback=2)
    assert [f.why for f in found] == ["무음이 7.5초 이어졌다", "대본을 만들지 못해 대체 멘트가 2번 나갔다"]


def test_repeated_phrase_ignores_words_given_by_listeners_and_titles(tmp_path):
    extra = [_speech(300 + n, f"{lead} 수능이 나흘 남은 오늘 힘내요.") for n, lead in enumerate(["자,", "그럼", "네,", "음,"])]
    found = _check(tmp_path, "repeated_phrase", [*PLAYED[:-1], *extra, PLAYED[-1]])
    assert len(found) == 1 and "멘트 4개" in found[0].why and found[0].quote in "수능이 나흘 남은 오늘 힘내요"
    titles = [_speech(300 + n, f"첫눈 오는 밤 {tail}") for n, tail in enumerate(["틀어 볼게요.", "어떠셨나요.", "좋았죠.", "또 듣고 싶네요."])]
    assert _check(tmp_path, "repeated_phrase", [*PLAYED[:-1], *titles, PLAYED[-1]]) == [], "곡 제목은 되풀이로 세지 않는다"


def test_sentence_tone_title_symbol_and_length_checks(tmp_path):
    long = "어제 저녁에 집에 돌아오는 길에 골목 어귀에서 우연히 마주친 오래된 가게의 간판을 한참 바라보다가 문득 어린 시절에 할머니 손을 잡고 걷던 시장 골목이 떠올랐어요."
    played = [*PLAYED[:4], _speech(25, long), _speech(26, "야 다들 뭐 하냐. 나 지금 신났어."),
              _speech(27, "오늘 기분 최고예요 😊 **진짜** http://example.com"), _speech(28, "ㅋㅋㅋ 정말 웃기네요."),
              _speech(29, "길게 말해 볼게요.", sec=48.0), *PLAYED[4:8],
              _speech(54, "이어서 들려드릴 곡은 첫눈이 오는 밤입니다.", "music_intro"), *PLAYED[9:]]
    b = load_broadcast(_run_dir(tmp_path, played))
    assert [f.at for f in CHECKS["long_sentence"](b)] == ["M5"]
    assert [f.at for f in CHECKS["speech_level"](b)] == ["M6"]
    assert [f.at for f in CHECKS["unreadable"](b)] == ["M7"], "ㅋㅋㅋ는 TTS 직전에 읽을 수 있게 바뀌므로 문제가 아니다"
    assert [f.at for f in CHECKS["long_ment"](b)] == ["M9"]
    assert [f.at for f in CHECKS["song_title"](b)] == ["M12"] and "첫눈 오는 밤 - 가상의 가수" in CHECKS["song_title"](b)[0].why


# ── 심사 AI의 답 확인 ──────────────────────────────────────────────────

def test_findings_must_point_at_the_record_and_quote_it(tmp_path):
    b = load_broadcast(_run_dir(tmp_path))
    ment = next(i for _, i in RUBRIC.pairs() if i.key == "A1")
    raw = {"findings": [
        {"at": "M4", "quote": "군고구마는 껍질을 벗길 때 김이 올라오죠", "why": "글말이다"},
        {"at": "M2", "quote": "붕어빵은 꼬리부터 드시는 분이 많죠", "why": "번호를 틀리게 적었다"},  # M10의 문장
        {"at": "M4", "quote": "기록에 없는 문장입니다", "why": "지어낸 근거"},
        {"at": "M99", "quote": "", "why": "없는 번호"},
        {"at": "M5", "quote": "호빵은 반으로 나눠 먹으면 더 맛있어요", "why": ""},
        "문자열",
    ]}
    found, dropped, na = J.check_findings(b, ment, raw)
    assert [(f.at, f.why) for f in found] == [("M4", "글말이다"), ("M10", "번호를 틀리게 적었다")] and dropped == 4 and not na
    chat = next(i for _, i in RUBRIC.pairs() if i.key == "B2")
    found, dropped, _ = J.check_findings(b, chat, {"findings": [
        {"at": "C2", "quote": "", "why": "받지 않았다"}, {"at": "C2", "quote": "", "why": "같은 자리"},
        {"at": "C9", "quote": "", "why": "없는 글"}, {"at": "C1", "quote": "지어낸 인용 문장", "why": "x"}]})
    assert [f.at for f in found] == ["C2"] and dropped == 2
    listener = next(i for _, i in RUBRIC.pairs() if i.key == "B6")
    found, dropped, _ = J.check_findings(b, listener, {"findings": [{"at": "L2", "why": "무시당했다"}, {"at": "L7", "why": "없는 청취자"}]})
    assert [f.at for f in found] == ["L2"] and dropped == 1
    assert J.check_findings(b, chat, {"na": True, "findings": [{"at": "C1", "why": "x"}]}) == ([], 0, True)
    with pytest.raises(ValueError, match="답이 없다"):
        J.check_findings(b, chat, None)
    assert J.parse_json('설명입니다 ```json\n{"A1": {"na": false}}\n``` 끝') == {"A1": {"na": False}}


# ── 채점 ──────────────────────────────────────────────────────────────

def test_clean_broadcast_scores_full_marks_and_asks_ten_calls(tmp_path):
    llm = FakeLlm()
    result = _judge(tmp_path, llm)
    assert result["total"] == 1.0 and len(llm.prompts) == 10 and all(s["passed"] for s in result["items"].values())
    assert result["criteria"]["B"] == {"name": "청취자 소통 및 공감력", "weight": 15, "score": 1.0, "passed": 8, "applicable": 8}
    b_prompt = next(p for p in llm.prompts if "[B1]" in p)
    assert "[B6]" not in b_prompt and "청취자별 보낸 글" not in b_prompt and "새벽" in b_prompt
    assert "청취자별 보낸 글" in next(p for p in llm.prompts if "[B6]" in p)
    blind = next(p for p in llm.prompts if '"pick"' in p)
    assert "○○" in blind and "이름: " not in blind and "금지 사항" not in blind and "지금 시작합니다" not in blind, \
        "이름과 오프닝·엔딩은 보여 주지 않는다"


def test_score_is_the_share_of_problem_free_items_weighted_by_criterion(tmp_path):
    llm = FakeLlm({
        "I4": {"findings": [{"at": "M9", "quote": "배고프다고 하신 분", "why": "맥락 없이 다시 꺼냈다"}]},
        "I1": {"findings": [{"at": "M8", "quote": "곡 잘 들으셨나요", "why": "앞과 이어지지 않는다"}]},  # 하나는 봐준다
        "B4": {"na": True, "findings": []}, "B5": {"na": True},
        "B2": {"findings": [{"at": "C3", "quote": "", "why": "받지 않았다"}]},
        "F3": {"na": True},
    })
    result = _judge(tmp_path, llm)
    items, criteria = result["items"], result["criteria"]
    assert items["I4"]["passed"] is False and items["I1"]["passed"] is True and items["I1"]["allowed"] == 1
    assert items["B4"]["passed"] is None and items["B4"]["na"]
    assert criteria["I"]["score"] == 4 / 5 and criteria["B"] == {**criteria["B"], "passed": 5, "applicable": 6}
    assert criteria["F"]["applicable"] == 2 and criteria["F"]["score"] == 1.0
    expected = (15 * 0.8 + 15 * 5 / 6 + 15 + 15 + 8 * 5) / 100
    assert result["total"] == pytest.approx(expected)


def test_code_findings_and_judge_findings_merge_on_the_same_item(tmp_path):
    long = "어제 저녁에 집에 돌아오는 길에 골목 어귀에서 우연히 마주친 오래된 가게의 간판을 한참 바라보다가 문득 어린 시절에 할머니 손을 잡고 걷던 시장 골목이 떠올랐어요."
    played = [*PLAYED[:4], _speech(25, long), *PLAYED[4:]]
    llm = FakeLlm({"A2": {"findings": [{"at": "M5", "quote": long[:20], "why": "절이 길게 이어진다"},
                                       {"at": "M4", "quote": "군고구마는 껍질을 벗길 때", "why": "어디서 쉴지 모르겠다"}]}})
    result = _judge(tmp_path, llm, played=played)
    assert [f["at"] for f in result["items"]["A2"]["findings"]] == ["M5", "M4"], "같은 멘트는 한 번만 센다"
    assert result["items"]["A2"]["passed"] is False and result["criteria"]["A"]["score"] == pytest.approx(2 / 3)


def test_wrong_blind_pick_is_a_personality_problem(tmp_path):
    result = _judge(tmp_path, FakeLlm({"pick": "천천히요"}))
    assert result["items"]["E2"]["passed"] is False and "고요(으)로 골랐다" in result["items"]["E2"]["findings"][0]["why"]
    assert result["criteria"]["E"]["score"] == pytest.approx(2 / 3)


def test_failed_call_leaves_its_items_out_of_the_score_and_bad_json_is_asked_again(tmp_path):
    result = _judge(tmp_path, FakeLlm(fail=("D1", "blind")))
    assert result["items"]["D1"]["error"] and result["items"]["D1"]["passed"] is None and result["items"]["E2"]["error"]
    assert result["criteria"]["D"]["applicable"] == 0 and result["criteria"]["D"]["score"] is None, "코드로 재는 D3도 심사 답이 없으면 뺀다"
    assert result["criteria"]["E"]["applicable"] == 2 and result["total"] == 1.0
    judge = J.Judge(J.Rubric(criteria=[RUBRIC.criteria[0]]), FakeLlm(garbage_first=True))
    again = asyncio.run(judge.judge(load_broadcast(_run_dir(tmp_path))))
    assert again["total"] == 1.0 and (judge.calls, judge.failures) == (2, 0)


def test_only_asks_the_named_criteria_and_reference_items_stay_out_of_the_score(tmp_path):
    llm = FakeLlm({"G1": {"findings": [{"at": "M4", "quote": "김이 올라오죠", "why": "x"}, {"at": "M5", "quote": "호빵은 반으로", "why": "y"}]}})
    b = load_broadcast(_run_dir(tmp_path))
    partial = asyncio.run(J.Judge(RUBRIC, llm).judge(b, {"G", "H"}))
    assert len(llm.prompts) == 1 and partial["items"]["A1"]["passed"] is None and partial["items"]["I5"]["passed"] is True
    assert partial["criteria"]["G"]["score"] == pytest.approx(2 / 3)
    data = RUBRIC.model_dump()
    for item in data["criteria"][-2]["items"]:
        item["reference"] = item["key"] == "G1"
    soft = asyncio.run(J.Judge(J.Rubric.model_validate(data), llm).judge(b, {"G", "H"}))
    assert soft["items"]["G1"]["passed"] is False and soft["criteria"]["G"] == {**soft["criteria"]["G"], "score": 1.0, "applicable": 2}


# ── 보고서 ────────────────────────────────────────────────────────────

def test_summary_and_stability(tmp_path):
    clean = _judge(tmp_path, FakeLlm())
    flawed = _judge(tmp_path, FakeLlm({"I4": {"findings": [{"at": "M9", "quote": "배고프다고 하신 분", "why": "맥락 없이 다시 꺼냈다"}]}}))
    text = J.render_summary(RUBRIC, [clean, flawed], notes=["- 심사 모델: 가짜"])
    assert "| aud_3 | 2 | **98** | 90 | 100 |" in text and "| I4 다시 꺼내기 | 심사 AI | 1/2 | 0 |" in text
    assert "**I4 다시 꺼내기** — 1건" in text and "M9: “배고프다고 하신 분” — 맥락 없이 다시 꺼냈다" in text
    assert "| A2 호흡 | 코드+심사 AI | 0/2 |" in text and "| I5 다시 시작 | 코드 | 0/2 |" in text
    result = J.stability(RUBRIC, [[clean, flawed, clean], [clean, clean, clean]])
    assert result["items"]["I4"] == {"name": "다시 꺼내기", "by": "judge", "judged": True, "same": 1, "broadcasts": 2, "agreement": 0.5}
    assert result["items"]["I1"]["agreement"] == 1.0 and result["total_spread_max"] == pytest.approx(0.03)
    table = J.render_stability(result, 3)
    assert "| I4 다시 꺼내기 | 1/2 |" in table and "| D3 되풀이 | 2/2 |" in table and "I5" not in table, "심사 AI에게 물은 항목만 적는다"


def test_find_runs_and_cost(tmp_path):
    a, b = _run_dir(tmp_path / "s" / "aud_3"), _run_dir(tmp_path / "s" / "aud_8", name="run-2")
    assert J.find_runs([tmp_path / "s", a]) == [a, b] and J.find_runs([tmp_path / "none"]) == []
    assert J.cost_usd(J.JudgeSpec(price_in=2, price_out=10), {"in": 1_000_000, "out": 100_000}) == pytest.approx(3.0)
    (a / "judge.json").write_text(json.dumps(_judge(tmp_path / "j", FakeLlm()), ensure_ascii=False), encoding="utf-8")
    assert J.cli([str(tmp_path / "s"), "--collect"]) == 0, "채점해 둔 결과만 모은다 — API 키가 없어도 된다"
    assert "이미 채점한 방송 1개" in (tmp_path / "s" / "judge.md").read_text(encoding="utf-8")


# ── 결함 주입 ──────────────────────────────────────────────────────────

LONG = [*PLAYED[:-1], *[_speech(230 + 10 * n, f"겨울 간식 이야기 {n}번째예요. 천천히 들어 보세요.") for n in range(14)],
        _speech(400, "오늘은 여기까지예요. 함께해 주셔서 고맙습니다.", "closing")]


def test_every_defect_can_be_planted_and_code_checks_catch_their_own(tmp_path):
    base = _run_dir(tmp_path, LONG, name="base")
    checks = {item.key: item for _, item in RUBRIC.pairs() if item.by != "judge"}
    assert len({d.key for d in D.DEFECTS}) == len(D.DEFECTS)
    covered = {key for d in D.DEFECTS for key in d.items[:1]}
    assert covered >= {item.key for _, item in RUBRIC.pairs() if item.by == "judge" and item.key != "F3"}, "심사 AI 항목마다 겨냥한 결함이 있다"
    for defect in D.DEFECTS:
        info = D.make_variant(base, defect, tmp_path / "out" / defect.key)
        assert info is not None, defect.key
        b = load_broadcast(tmp_path / "out" / defect.key)
        target = defect.items[0]
        if target in checks:
            found = CHECKS[checks[target].check](b, **checks[target].params)
            assert found, f"{defect.key}: {target} 검사가 잡아야 한다"
    assert json.loads((base / "played.json").read_text(encoding="utf-8"))["played"] == LONG, "원본은 바꾸지 않는다"


def test_defect_is_caught_only_when_a_finding_points_at_it(tmp_path):
    base = _run_dir(tmp_path, LONG, name="base")
    grammar = next(d for d in D.DEFECTS if d.key == "grammar")
    info = D.make_variant(base, grammar, tmp_path / "out" / "grammar")
    b = load_broadcast(tmp_path / "out" / "grammar")
    spot = next(m for m in b.ments if m.script in info["marks"])
    hit = FakeLlm({"G1": {"findings": [{"at": spot.id, "quote": "붕어빵를 먹었는데", "why": "조사가 틀렸다"}]}})
    miss = FakeLlm({"G1": {"findings": [{"at": "M4", "quote": "김이 올라오죠", "why": "다른 자리"}]}})
    judged = [asyncio.run(J.Judge(RUBRIC, llm).judge(b, {"G"})) for llm in (hit, miss)]
    assert D.caught(info, judged[0], None, b.path) == ["G1"] and D.caught(info, judged[1], None, b.path) == []
    other = next(d for d in D.DEFECTS if d.key == "other_persona")
    info = D.make_variant(base, other, tmp_path / "out" / "other")
    swapped = load_broadcast(tmp_path / "out" / "other")
    assert swapped.persona["persona_id"] == "beongae"
    result = asyncio.run(J.Judge(RUBRIC, FakeLlm()).judge(swapped, {"E"}))
    assert D.caught(info, result, None, swapped.path) == ["E2"], "대본은 새벽인데 설정은 번개다 — 가려 놓고 고르면 새벽을 고른다"
    text = D.render([{"name": "틀린 조사와 맞춤법", "items": ["G1"], "status": "잡음", "hits": ["G1"], "why": "조사가 틀렸다"}],
                    base, "호출 1번")
    assert "| 틀린 조사와 맞춤법 | G1 | 잡음 | G1 | 조사가 틀렸다 |" in text and "겨냥한 항목이 잡은 것 1개" in text


def test_random_is_seeded_for_the_blind_options(tmp_path):
    b = load_broadcast(_run_dir(tmp_path, LONG))
    first = J.build_blind_prompt(b, J.load_presets(), random.Random("1"))[1]
    assert first == J.build_blind_prompt(b, J.load_presets(), random.Random("1"))[1] and set(first.values()) == set(J.load_presets())
    custom = load_broadcast(_run_dir(tmp_path, LONG, name="custom", persona=None))
    assert J.build_blind_prompt(custom, J.load_presets(), random.Random(1)) is None, "프리셋이 아닌 DJ는 해당 없음이다"
