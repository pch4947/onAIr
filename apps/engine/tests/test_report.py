"""방송 1회 정량 리포트 — 집계 값과 CLI (2026-11-07 회의 7번)."""
import asyncio
import json
from pathlib import Path

import pytest

from onair_engine.domain import StationConfig, StationProfile
from onair_engine.engine import EngineSettings, StationEngine
from onair_engine.report import cli, render, script_stats, station_report
from onair_engine.telemetry import Telemetry

ROOT = Path(__file__).resolve().parent.parent
PROFILE = StationProfile(dj_name="테스트", tone="차분한 존댓말", concept="테스트 방송")


class Silent:
    async def publish_segment(self, sub):
        pass

    async def notify_request_state(self, request_id, state):
        pass

    async def notify_station(self, event_type, payload):
        pass


@pytest.fixture(scope="module")
def broadcast(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("report")
    cfg = StationConfig(station_id="st_report", profile=PROFILE, broadcast_minutes=30, format="talk",
                        topic="겨울 간식", corners=["text_corner", "topic_talk"])
    settings = EngineSettings(audio_dir=tmp / "audio", sqlite_path=tmp / "m.sqlite",
                              safety_rules_path=ROOT / "config" / "safety_rules.yaml",
                              chat_rules_path=ROOT / "config" / "chat_rules.yaml",
                              formats_dir=ROOT / "config" / "formats", target_buffer_sec=1e9)
    engine = StationEngine(cfg, settings, Silent())

    async def scenario():
        await engine.submit_request("message", "오늘 붕어빵 먹었어요", "u1", nickname="밤샘러")
        await engine.submit_request("story", "호떡이 더 좋아요", "u2")
        await engine.run(max_segments=12)

    asyncio.run(scenario())
    return tmp / "m.sqlite"


def test_report_counts_a_broadcast(broadcast):
    report = station_report(broadcast, "st_report")
    assert report["topic"] == "겨울 간식"
    assert report["inputs"] == {"message": {"inbox": 1}, "chat": {"pool": 1}}  # v1의 story는 채팅이다
    assert report["corners"]["opening"]["enter"] == 1
    gen = report["generation"]
    assert gen["jobs"] > 0 and gen["outcomes"]["ok"] == gen["jobs"]
    assert set(gen["stage_sec"]) == {"llm", "l2", "tts"}
    assert gen["job_sec"]["count"] == gen["jobs"] and gen["job_sec"]["p50"] is not None
    assert report["corner_rates"]["turns"] >= len(report["corners"])


def test_render_has_every_section(broadcast):
    text = render(station_report(broadcast, "st_report"))
    for heading in ("# 방송 리포트 · st_report", "## 입력", "## 반영", "## 코너", "## 생성", "## 그 밖의 반영"):
        assert heading in text
    assert "| opening | 1 |" in text


def test_outcomes_and_rates_from_synthetic_log(tmp_path):
    telemetry = Telemetry(tmp_path / "m.sqlite", "st_x")
    for job, stages in {
        "ok": [("llm", "ok", 1.0), ("l2", "ok", 0.0), ("tts", "ok", 2.0)],
        "blocked": [("llm", "ok", 1.0), ("l2", "reject", 0.0)],
        "filler": [("llm", "error", 5.0), ("fallback", "filler", 0.0), ("tts", "ok", 0.5)],
    }.items():
        for stage, result, sec in stages:
            telemetry.log_stage(job_id=job, corner_type="topic_talk", request_ref=None, stage=stage,
                                started_at=0.0, finished_at=sec, result=result)
    for event in ("enter", "exit", "skip", "fallback", "enter", "exit"):
        telemetry.log_corner("story_corner" if event in ("skip", "fallback") else "topic_talk", None,
                             event, 0 if event == "enter" else 60_000)
    telemetry.close()

    report = station_report(tmp_path / "m.sqlite", "st_x")
    gen = report["generation"]
    assert gen["outcomes"] == {"ok": 1, "fallback": 1, "reject": 1, "incomplete": 0, "error": 1}
    assert gen["fallback_rate"] == pytest.approx(0.333)
    assert gen["job_sec"] == {"count": 1, "p50": 3.0, "p95": 3.0}, "대체·거부는 지연에 넣지 않는다"
    assert report["corner_rates"] == {"turns": 3, "skip_rate": 0.333, "fallback_rate": 0.333}
    assert report["corners"]["topic_talk"]["produced_sec"]["p50"] == 60.0


def test_script_stats_catches_repetition_and_topic_reintroduction():
    # 1차 시나리오 실행의 quiet 대본처럼 — 매번 "겨울 간식은 …"으로 시작하고 주제를 다시 소개한다 (#222)
    repetitive = [
        "오늘은 겨울 간식 이야기를 나눠 볼게요. 손이 시린 밤이네요.",
        "겨울 간식은 손끝을 녹여 주죠. 어떤 간식이 떠오르세요?",
        "겨울 간식은 봉지를 열 때 냄새가 좋죠. 어떤 간식이 떠오르세요?",
        "오늘은 겨울간식 이야기를 해 볼게요. 요즘 생각나는 간식은요?",
    ]
    varied = [
        "오늘은 겨울 간식 이야기를 나눠 볼게요.",
        "붕어빵은 꼬리부터 드세요, 머리부터 드세요?",
        "편의점 호빵은 전자레인지에 물을 살짝 뿌려 데우면 촉촉해요.",
        "아까 밤샘러 님이 퇴근길 군고구마 얘기를 해 주셨죠.",
    ]
    bad, good = script_stats(repetitive, "겨울 간식"), script_stats(varied, "겨울 간식")
    assert bad["same_opening_rate"] == 0.5 and good["same_opening_rate"] == 0
    assert bad["topic_intros"] == 2, "띄어쓰기가 달라도 같은 주제 소개다"
    assert good["topic_intros"] == 1, "오프닝의 소개 1번이 정상이다"
    assert bad["similarity_mean"] > good["similarity_mean"]
    assert script_stats([], "겨울 간식")["same_opening_rate"] is None
    assert script_stats(varied, None)["topic_intros"] is None


def test_report_has_script_section(broadcast):
    report = station_report(broadcast, "st_report")
    assert report["scripts"]["segments"] == report["aired"]["segments"]
    assert "## 멘트" in render(report)


def test_corner_length_prefers_the_corners_own_speech(tmp_path):
    # exit − enter에는 앞 코너에서 소개한 곡이 섞인다 — 코너가 낸 멘트 길이(length)가 있으면 그것을 쓴다 (#220)
    telemetry = Telemetry(tmp_path / "m.sqlite", "st_x")
    for event, ms in (("enter", 0), ("exit", 250_000), ("length", 45_000)):
        telemetry.log_corner("topic_talk", None, event, ms)
    telemetry.close()
    corner = station_report(tmp_path / "m.sqlite", "st_x")["corners"]["topic_talk"]
    assert corner["produced_sec"]["p50"] == 45.0
    assert "length" not in corner, "진입·종료 횟수에 섞지 않는다"


def test_reused_station_id_is_split_into_runs(tmp_path):
    # 백엔드는 고정 채널 ID를 다시 쓴다 — 회차를 섞으면 지표가 의미 없다
    telemetry = Telemetry(tmp_path / "m.sqlite", "st_ch")
    db = telemetry._db
    rows = [("station.created", 100.0), ("station.started", 110.0),
            ("station.created", 200.0), ("station.started", 210.0), ("station.started", 300.0)]
    for event, at in rows:
        db.execute("INSERT INTO station_log VALUES (?,?,?,?)",
                   ("st_ch", at, event, json.dumps({"topic": f"주제{at:.0f}"})))
    for job, at in (("a", 120.0), ("b", 205.0), ("c", 220.0), ("d", 310.0)):
        db.execute("INSERT INTO generation_log VALUES (?,?,?,?,?,?,?,?)",
                   (job, "st_ch", "topic_talk", None, "tts", at, at + 1, "ok"))
    db.commit()
    telemetry.close()

    reports = [station_report(tmp_path / "m.sqlite", "st_ch", run) for run in range(3)]
    assert [r["generation"]["jobs"] for r in reports] == [1, 2, 1], "생성 전(created~started) 기록도 그 회차다"
    assert [r["topic"] for r in reports] == ["주제110", "주제210", "주제300"]
    assert station_report(tmp_path / "m.sqlite", "st_ch")["run"] == {"index": 2, "count": 3}


def test_cli_lists_stations_and_prints_json(broadcast, capsys):
    assert cli(["--db", str(broadcast)]) == 0
    assert "st_report" in capsys.readouterr().out
    assert cli(["st_report", "--db", str(broadcast), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["station_id"] == "st_report"
    assert cli(["st_report", "--db", str(broadcast.parent / "none.sqlite")]) == 1
    assert cli(["st_report", "--db", str(broadcast), "--run", "2"]) == 1, "회차가 하나뿐이다"
