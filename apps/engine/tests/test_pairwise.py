"""대본 쌍 비교 — 짝짓기, 가리기, 집계, CLI (실제 API 호출 없음)."""
import asyncio
import json
import random

import pytest

from onair_engine.eval import pairwise
from onair_engine.pipeline.llm import LlmError
from onair_engine.prompt_data import data_block


def _rec(step, text, *, outcome="aired", corner="talk", persona="saebyeok", run=0):
    return {"persona_file": persona, "persona": "새벽", "run": run, "step": step, "corner": corner, "case": None,
            "outcome": outcome, "text": text, "error": None, "latency": 1.0, "flags": [], "sentences": 1,
            "level_violations": 0, "similarity": 0.0, "signature": False, "says_name": False,
            "prompt_user": "토크를 작성하라.\n" + data_block("지금 화제", "겨울 간식")}


def _write(folder, records):
    folder.mkdir()
    (folder / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
                                          encoding="utf-8")
    (folder / "run.json").write_text(json.dumps({"git": "abc1234", "git_dirty": True, "llm": "openai gpt"}),
                                     encoding="utf-8")
    return folder


def test_pairs_match_position_and_hide_which_side_is_new():
    a = [_rec(0, "옛 대본 0"), _rec(1, "옛 대본 1"), _rec(2, None, outcome="l1_reject"), _rec(3, "같은 대본"),
         _rec(4, "옛 대본 4"), _rec(5, "옛 대본 5", corner="story")]
    b = [_rec(0, "새 대본 0"), _rec(1, "새 대본 1"), _rec(2, "새 대본 2"), _rec(3, "같은 대본"),
         _rec(5, "새 대본 5", corner="music_intro")]
    pairs, first_is, skipped = pairwise.make_pairs(a, b, random.Random(3))
    assert [p["id"] for p in pairs] == ["p001", "p002"]
    assert skipped == {"짝 없음": 2, "A만 송출": 0, "B만 송출": 1, "둘 다 미송출": 0, "대본 같음": 1}
    for p in pairs:
        old, new = (p["first"], p["second"]) if first_is[p["id"]] == "A" else (p["second"], p["first"])
        assert old.startswith("옛") and new.startswith("새")
        assert p["material"].startswith("<data") and "작성하라" not in p["material"], "지시문은 심사에 보여 주지 않는다"
    orders = {tuple(pairwise.make_pairs(a, b, random.Random(s))[1].values()) for s in range(20)}
    assert len(orders) > 1, "보여 주는 순서는 seed마다 섞인다"
    assert "새 대본" in pairwise.render_pairs(pairs) and "key.json" in pairwise.render_pairs(pairs)


def test_sign_test():
    assert pairwise.sign_test(6, 0) == pytest.approx(2 / 64)
    assert pairwise.sign_test(5, 0) == pytest.approx(2 / 32)
    assert pairwise.sign_test(5, 5) == 1.0 and pairwise.sign_test(0, 0) is None


def test_tally_maps_choices_back_to_before_and_after():
    pairs = [{"id": f"p{i}", "corner": "talk" if i < 6 else "story", "persona": "새벽"} for i in range(8)]
    first_is = {f"p{i}": "A" if i % 2 else "B" for i in range(8)}
    # B(고친 뒤)를 여섯 번, 같음 한 번, 하나는 고르지 않았다
    votes = {f"p{i}": (pairwise.SECOND if first_is[f"p{i}"] == "A" else pairwise.FIRST) for i in range(6)}
    votes["p6"] = pairwise.TIE
    result = pairwise.tally(pairs, first_is, votes)
    assert result["total"] == {"A": 0, "B": 6, pairwise.TIE: 1} and result["judged"] == 7
    assert result["verdict"] == "B가 낫다" and result["p_value"] == pytest.approx(2 / 64)
    assert result["by_corner"]["story"] == {"A": 0, "B": 0, pairwise.TIE: 1}
    assert result["first_chosen_rate"] == 0.5, "고른 쪽이 순서와 무관하다"

    few = pairwise.tally(pairs, first_is, {k: v for k, v in list(votes.items())[:5]})
    assert few["verdict"] == "표본 부족", "다섯 번을 다 이겨도 p = 0.06이다"
    split = {f"p{i}": pairwise.FIRST for i in range(8)}  # 먼저 보인 쪽만 고르면 A 4, B 4
    mixed = pairwise.tally(pairs, first_is, split)
    assert mixed["verdict"] == "차이 없음" and mixed["first_chosen_rate"] == 1.0


def test_votes_file_accepts_common_spellings(tmp_path):
    pairs = [{"id": f"p{i}"} for i in range(5)]
    pairwise.write_votes(tmp_path / "votes.csv", pairs, {"p0": ("가", "더 자연스럽다")})
    text = (tmp_path / "votes.csv").read_text(encoding="utf-8-sig").splitlines()
    assert text[0] == "id,선택,메모" and text[1] == "p0,가,더 자연스럽다" and text[2] == "p1,,"
    (tmp_path / "votes.csv").write_text("id,선택,메모\np0,가,\np1, B ,\np2,=,\np3,,\np4,모름,\n", encoding="utf-8-sig")
    assert pairwise.read_votes(tmp_path / "votes.csv") == {"p0": "가", "p1": "나", "p2": "같음"}


def test_judge_reads_one_line_answers_and_survives_failures():
    assert pairwise.parse_choice("선택 나 근거 인사가 더 자연스럽다") == ("나", "인사가 더 자연스럽다")
    assert pairwise.parse_choice("선택: 같음") == ("같음", "") and pairwise.parse_choice("잘 모르겠다") is None

    class Judge:
        def __init__(self):
            self.prompts = []

        async def complete(self, prompt, *, max_tokens):
            self.prompts.append(prompt)
            if len(self.prompts) == 2:
                raise LlmError("일시 오류")
            return "선택 가 근거 짧고 분명하다" if len(self.prompts) == 1 else "형식이 다른 답"

    judge = Judge()
    pairs = [{"id": f"p{i}", "persona": "새벽", "persona_file": "saebyeok", "corner": "talk",
              "material": "", "first": f"대본 {i}", "second": "다른 대본"} for i in range(3)]
    votes = asyncio.run(pairwise.judge_pairs(judge, pairs))
    assert votes == {"p0": ("가", "짧고 분명하다")}, "실패하거나 형식이 틀린 쌍은 고르지 않은 것으로 남는다"
    assert "대본 0" in judge.prompts[0].user and "이름:" in judge.prompts[0].user, "persona 설정을 같이 보여 준다"


def test_cli_makes_blind_pairs_then_tallies_filled_votes(tmp_path, capsys):
    a = _write(tmp_path / "before", [_rec(i, f"옛 대본 {i}") for i in range(7)])
    b = _write(tmp_path / "after", [_rec(i, f"새 대본 {i}") for i in range(7)])
    out = tmp_path / "pairs"
    assert pairwise.cli([str(a), str(b), "--out", str(out)]) == 0
    assert "쌍 7개" in capsys.readouterr().out
    key = json.loads((out / "key.json").read_text(encoding="utf-8"))
    assert key["judge"] is None and set(key["first_is"].values()) <= {"A", "B"}
    blind = (out / "pairs.md").read_text(encoding="utf-8")
    assert "before" not in blind and "after" not in blind, "쌍 파일만 봐서는 어느 쪽인지 알 수 없다"

    # 사람이 채운 것처럼 — 전부 고친 뒤(B)를 골랐다
    rows = ["id,선택,메모"] + [f"{pid},{'나' if side == 'A' else '가'}," for pid, side in key["first_is"].items()]
    (out / "votes.csv").write_text("\n".join(rows) + "\n", encoding="utf-8-sig")
    assert pairwise.cli(["--tally", str(out)]) == 0
    assert "B가 낫다" in capsys.readouterr().out
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "**판정: B가 낫다** — B 7 · A 0 · 같음 0" in report and "커밋 abc1234 + 커밋 안 한 변경" in report
    assert "| 송출 | 7 | 7 |" in report, "규칙 위반은 자동 지표로 따로 본다"

    assert pairwise.cli(["--tally", str(tmp_path)]) == 1
    assert pairwise.cli([str(a), str(a), "--out", str(tmp_path / "same")]) == 1, "대본이 전부 같으면 견줄 것이 없다"
