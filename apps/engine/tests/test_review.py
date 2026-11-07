"""대본 검토 페이지 — 실행 결과 모으기, 예전 회차 대본 읽기, HTML 한 장 (#229)."""
import json

from onair_engine.scenario import review

TRANSCRIPT = """# 대본 · quiet

시각은 방송 시작(station.started) 기준이다.

- `0:09` **[오프닝]** (8초)
  안녕하세요, 새벽입니다.
- `0:40` **[오늘의 이야기]** (3초)
  (고정 문구 또는 엔진 종료 뒤 송출)
- `1:30` ▶ 입력 · 채팅 in_tired (a): 오늘 너무 피곤해요
- `1:34` **[곡 소개]** (10초) · 반영: in_song, in_i2
  피곤한직장인 님의 신청곡입니다.
- `1:44` ♪ 곡 · 첫눈 - 가상의 가수 (3:30)
- `5:14` ⏸ **무음 2.5초**
"""


def test_parse_transcript_reads_every_line_kind():
    played = review.parse_transcript(TRANSCRIPT)
    assert [p["kind"] for p in played] == ["speech", "speech", "input", "speech", "music", "silence"]
    assert played[0] == {"at": 9, "kind": "speech", "sec": 8, "corner": "오프닝",
                         "script": "안녕하세요, 새벽입니다.", "reflected": []}
    assert played[1]["script"] is None, "고정 문구는 대본 기록이 없다"
    assert played[3]["reflected"] == ["in_song", "in_i2"]
    assert played[4] == {"at": 104, "kind": "music", "sec": 210, "text": "첫눈 - 가상의 가수"}
    assert played[5]["sec"] == 2.5


def _run(root, scenario, run, *, played=None, transcript=None, report=None):
    d = root / scenario / run
    d.mkdir(parents=True)
    if played is not None:
        (d / "played.json").write_text(json.dumps({"played": played}, ensure_ascii=False), encoding="utf-8")
    if transcript is not None:
        (d / "transcript.md").write_text(transcript, encoding="utf-8")
    (d / "report.json").write_text(json.dumps(report or {}, ensure_ascii=False), encoding="utf-8")
    (d / "run.json").write_text(json.dumps({"git": "abc1234", "llm": {"model": "m"}, "tts": "sim",
                                            "scenario": {"description": "설명"}}), encoding="utf-8")


def test_collect_prefers_played_json_and_falls_back_to_transcript(tmp_path):
    new = [{"at": 0, "kind": "speech", "sec": 5, "corner": "오프닝", "script": "새 기록"}]
    _run(tmp_path, "quiet", "run-1", played=new, transcript=TRANSCRIPT,
         report={"topic": "겨울 간식", "sim": {"talk_ratio": 0.2, "silence_total_sec": 0},
                 "expect": [{"passed": True}, {"passed": False}]})
    _run(tmp_path, "sparse", "run-1", transcript=TRANSCRIPT)
    runs = review.collect(tmp_path, "수정 후")
    assert [(r["scenario"], r["run"], r["source"]) for r in runs] == [("quiet", "run-1", "수정 후"),
                                                                        ("sparse", "run-1", "수정 후")]
    assert runs[0]["played"] == new
    assert runs[0]["summary"]["talk_ratio"] == 0.2 and runs[0]["summary"]["expect"] == "1/2"
    assert runs[0]["summary"]["topic"] == "겨울 간식" and runs[0]["git"] == "abc1234"
    assert len(runs[1]["played"]) == 6
    assert [r["scenario"] for r in review.collect(tmp_path / "quiet")] == ["quiet"], "시나리오 폴더도 받는다"


def test_page_embeds_data_without_breaking_out_of_the_script(tmp_path):
    hostile = [{"at": 0, "kind": "speech", "sec": 5, "corner": "오프닝",
                "script": "</script><script>alert(1)</script>"}]  # 대본은 LLM 출력이다
    _run(tmp_path, "quiet", "run-1", played=hostile)
    html = review.render(review.collect(tmp_path))
    assert "/*__REVIEW_DATA__*/" not in html
    data = html.split('<script id="review-data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert json.loads(data)["runs"][0]["played"][0]["script"] == hostile[0]["script"]


def test_cli_writes_one_page_for_several_folders(tmp_path, capsys):
    before, after = tmp_path / "before", tmp_path / "after"
    _run(before, "quiet", "run-1", transcript=TRANSCRIPT)
    _run(after, "quiet", "run-1", transcript=TRANSCRIPT)
    out = tmp_path / "review.html"
    assert review.cli([str(before), str(after), "--label", "전", "--label", "후", "-o", str(out)]) == 0
    assert "회차 2개" in capsys.readouterr().out
    assert '"source": "전"' in out.read_text(encoding="utf-8")
    assert review.cli([str(tmp_path / "none")]) == 1
    (tmp_path / "empty").mkdir()
    assert review.cli([str(tmp_path / "empty")]) == 1
