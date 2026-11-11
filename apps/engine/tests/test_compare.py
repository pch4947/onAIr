"""실행 결과 비교 — 짝 비교 판정, 회차 간 편차 표, CLI (docs/ENGINE_TUNING.md)."""
import json

import pytest

from onair_engine.scenario import compare


def _run(root, scenario, index, *, silence=0.0, rate=None, talk=0.2, error=None, seed=None):
    run_dir = root / scenario / f"run-{index}"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(json.dumps({
        "sim": {"silence_total_sec": silence, "talk_ratio": talk, "error": error},
        "reflection": {"standalone_reflection_rate": rate},
    }), encoding="utf-8")
    (run_dir / "run.json").write_text(json.dumps({
        "seed": index if seed is None else seed, "git": "abc1234", "git_dirty": False,
        "llm": {"kind": "dummy", "model": None}, "tts": "sim", "overrides": {},
    }), encoding="utf-8")


def _rows(result, scenario="s"):
    return {row["key"]: row for row in result[scenario]["rows"]}


def test_t_threshold_shrinks_with_more_runs():
    assert compare.t95(2) == pytest.approx(4.303) and compare.t95(4) == pytest.approx(2.776)
    assert compare.t95(100) == 1.96


def test_paired_verdicts(tmp_path):
    base, cand = tmp_path / "base", tmp_path / "cand"
    for i, (b_sil, c_sil, b_rate, c_rate, b_talk, c_talk) in enumerate([
        (10.0, 5.0, 0.50, 0.75, 0.20, 0.30),
        (12.0, 6.0, 0.60, 0.40, 0.21, 0.31),
        (11.0, 5.5, 0.40, 0.45, 0.19, 0.29),
        (13.0, 6.5, 0.55, 0.50, 0.20, 0.30),
    ], start=1):
        _run(base, "s", i, silence=b_sil, rate=b_rate, talk=b_talk)
        _run(cand, "s", i, silence=c_sil, rate=c_rate, talk=c_talk)
    rows = _rows(compare.compare(compare.load(base), compare.load(cand)))

    silence = rows["silence_total_sec"]
    assert silence["verdict"] == compare.IMPROVED, "낮을수록 좋은 지표가 모든 짝에서 줄었다"
    assert (silence["n"], silence["wins"], silence["losses"]) == (4, 4, 0)
    assert silence["diff"] == pytest.approx(-5.75) and silence["base"]["mean"] == pytest.approx(11.5)
    assert rows["standalone_reflection_rate"]["verdict"] == compare.SAME, "오르락내리락한 차이는 흔들림이다"
    assert rows["talk_ratio"]["verdict"] == compare.NOTE, "방향이 없는 지표는 판정하지 않는다"
    assert rows["talk_ratio"]["wins"] is None

    worse = _rows(compare.compare(compare.load(cand), compare.load(base)))
    assert worse["silence_total_sec"]["verdict"] == compare.WORSE


def test_too_few_pairs_errors_and_seed_mismatch(tmp_path):
    base, cand = tmp_path / "base", tmp_path / "cand"
    for i in (1, 2, 3):
        _run(base, "s", i, silence=10.0 + i)
        _run(cand, "s", i, silence=1.0, error="boom" if i == 3 else None, seed=i if i == 1 else 99)
    _run(base, "only_base", 1)
    result = compare.compare(compare.load(base), compare.load(cand))
    assert list(result) == ["s"], "양쪽에 다 있는 시나리오만 견준다"
    block = result["s"]
    assert (block["pairs"], block["skipped_errors"], block["same_seeds"]) == (2, 1, False)
    assert _rows(result)["silence_total_sec"]["verdict"] == compare.FEW, "오류 회차를 빼면 두 쌍뿐이다"
    text = compare.render_compare(base, cand, result)
    assert "오류 회차 1쌍 제외" in text and "seed가 달라" in text


def test_render_lists_moved_metrics_and_folds_identical_ones(tmp_path):
    base, cand = tmp_path / "base", tmp_path / "cand"
    for i in (1, 2, 3):
        _run(base, "s", i, silence=10.0 + i, talk=0.2)
        _run(cand, "s", i, silence=4.0 + i / 2, talk=0.2)
    text = compare.render_compare(base, cand, compare.compare(compare.load(base), compare.load(cand)),
                                  base_label="수정 전", cand_label="수정 후")
    assert "# 비교 · 수정 전 → 수정 후" in text and "커밋 abc1234" in text
    assert "- 개선: s 무음 합계(초)(-7.00)" in text and "- 악화: 없음" in text
    assert "| 무음 합계(초) ↓ | 12 ± 1 | 5 ± 0.50 | -7.00 | 3/0 | **개선** |" in text
    assert "모든 회차에서 양쪽이 같은 지표: 말 비율 0.20" in text, "움직이지 않은 지표는 표를 채우지 않는다"


def test_noise_table_says_how_big_a_difference_is_detectable(tmp_path):
    for i, silence in enumerate((10.0, 12.0, 11.0, 13.0), start=1):
        _run(tmp_path, "s", i, silence=silence)
    text = compare.render_noise(tmp_path, compare.load(tmp_path))
    # 표준편차 1.29, n=4 → t(6)=2.447 × 1.29 × √(2/4) = 2.23
    assert "| 무음 합계(초) ↓ | 4 | 11.50 | 1.29 | 10 | 13 | 2.23 |" in text


def test_cli_noise_compare_and_sweep_folder(tmp_path, capsys):
    out = tmp_path / "20261110-000000"
    for name, silence in (("base", 10.0), ("flood_threshold=5", 4.0)):
        for i in (1, 2, 3):
            _run(out / name, "s", i, silence=silence + i)
    (out / "session.json").write_text(json.dumps(
        {"git": "abc1234", "variants": {"base": {}, "flood_threshold=5": {"chat.flood_threshold": 5}}}),
        encoding="utf-8")

    assert compare.cli([str(out / "base")]) == 0
    assert "회차 간 편차" in capsys.readouterr().out

    table, data = tmp_path / "t.md", tmp_path / "t.json"
    assert compare.cli([str(out), "-o", str(table), "--json", str(data)]) == 0, "변형 실행 폴더는 base 대비로 펼친다"
    printed = capsys.readouterr().out
    assert "# 비교 · base → flood_threshold=5" in printed and table.read_text(encoding="utf-8") == printed
    assert "`20261110-000000/base`" in printed, "출처에 실행 시각과 변형 이름이 함께 나온다"
    rows = json.loads(data.read_text(encoding="utf-8"))["candidates"][str(out / "flood_threshold=5")]["s"]["rows"]
    assert next(r for r in rows if r["key"] == "silence_total_sec")["verdict"] == compare.IMPROVED

    assert compare.write_variants(out, ["base", "flood_threshold=5"]) == out / "compare.md"
    assert "## 비교 · base → flood_threshold=5" in (out / "compare.md").read_text(encoding="utf-8")
    assert compare.write_variants(out, ["base"]) is None

    assert compare.cli([str(tmp_path / "missing")]) == 1


def test_refresh_fills_metrics_added_after_the_run(tmp_path, capsys):
    # 지난 실행의 report.json에는 그 뒤에 만든 지표가 없다 — 회차의 계측 DB에서 계산해 채운다 (#262)
    from onair_engine.domain import Decision, ScheduleContext
    from onair_engine.telemetry import Telemetry

    _run(tmp_path, "s", 1, silence=1.5)
    run_dir = tmp_path / "s" / "run-1"
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    report["station_id"] = "sim_s_1"
    report["scripts"] = {"segments": 99}  # 이미 있는 값은 건드리지 않는다
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    telemetry = Telemetry(run_dir / "metrics.sqlite", "sim_s_1")
    ctx = ScheduleContext(now=0.0, pending_requests=[], generated_buffer_sec=0, inflight_generations=0,
                          backpressure=False, corner=None, step_index=0, remaining_sec=0)
    for _ in range(2):
        telemetry.log_decision(ctx, Decision(action="glue", corner_type="topic_talk", step="glue:resume"))
    telemetry.log_aired("seg_1", 1.0, "어떤 간식이 떠오르세요?")
    telemetry.log_aired("seg_2", 2.0, "저는 군고구마가 좋아요.")
    telemetry.close()
    _run(tmp_path, "s", 2)  # 계측 DB를 지운 회차

    assert compare.refresh(tmp_path) == (1, 1)
    filled = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    assert filled["glue"]["resume"] == 2 and filled["scripts"] == {
        "segments": 99, "same_opening_rate": 0.0, "similarity_mean": 0.0, "topic_intros": None,
        "question_end_rate": 0.5}
    assert filled["sim"]["silence_total_sec"] == 1.5, "흉내 백엔드가 잰 값은 그대로다"
    assert compare.refresh(tmp_path) == (0, 1), "두 번 채우지 않는다"
    assert compare.cli([str(tmp_path), "--refresh"]) == 0
    assert "돌아왔어요 문구" in capsys.readouterr().out
