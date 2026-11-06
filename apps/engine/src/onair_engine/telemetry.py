"""계측 — 상태 전이·생성 지연·정책 결정 기록 (설계 문서 7장).

실험 데이터(RQ-A·RQ-B)의 원천이므로 부가 기능이 아니라 항상 켜져 있는 기본 동작이다.
백엔드/프론트 계측과의 조인 키는 segment_id / request_id.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS generation_log (
    job_id TEXT, station_id TEXT, corner_type TEXT, request_ref TEXT,
    stage TEXT, started_at REAL, finished_at REAL, result TEXT
);
CREATE TABLE IF NOT EXISTS request_log (
    request_id TEXT, station_id TEXT, state TEXT, at REAL
);
CREATE TABLE IF NOT EXISTS decision_log (
    station_id TEXT, at REAL, context_json TEXT, decision_json TEXT
);
CREATE TABLE IF NOT EXISTS corner_log (
    station_id TEXT, at REAL, corner_type TEXT, label TEXT, event TEXT, produced_ms INTEGER
);
CREATE TABLE IF NOT EXISTS station_log (
    station_id TEXT, at REAL, event TEXT, payload_json TEXT
);
CREATE TABLE IF NOT EXISTS input_log (
    request_id TEXT, station_id TEXT, received_at REAL, chat_type TEXT, rule TEXT,
    anchor_id TEXT, anchor_kind TEXT, input_kind TEXT, route TEXT
);
CREATE TABLE IF NOT EXISTS selection_log (
    station_id TEXT, at REAL, situation TEXT, criterion TEXT, arrivals INTEGER,
    anchor_id TEXT, candidates_json TEXT, picked_json TEXT
);
CREATE TABLE IF NOT EXISTS reflection_log (
    request_id TEXT, station_id TEXT, segment_id TEXT, chat_type TEXT, received_at REAL,
    started_at REAL, quote_age REAL, quote_age_net REAL, topic_match INTEGER,
    script_chars INTEGER, quote_chars INTEGER
);
CREATE TABLE IF NOT EXISTS steer_log (
    request_id TEXT, station_id TEXT, segment_id TEXT, at REAL, corner_type TEXT, rule TEXT,
    old_topic TEXT, new_topic TEXT
);
CREATE TABLE IF NOT EXISTS aired_log (
    station_id TEXT, segment_id TEXT, at REAL, script TEXT
);
"""


def _dump(obj) -> str:
    return json.dumps(dataclasses.asdict(obj), ensure_ascii=False, default=str)


def _read(path: Path, sql: str, params: tuple) -> list[tuple]:
    """읽기 전용 연결로 조회. 아직 아무 방송도 기록하지 않아 테이블이 없으면 빈 결과다."""
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return []
    try:
        return db.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    finally:
        db.close()


def read_decisions(sqlite_path: str | Path, station_id: str, after: int = 0,
                   limit: int = 100) -> list[dict]:
    """결정 로그 페이지 — rowid가 커서다 (계약 6.1절). 쓰기 연결과 별도로 읽는다."""
    path = Path(sqlite_path)
    if not path.exists():
        return []
    rows = _read(path, "SELECT rowid, at, context_json, decision_json FROM decision_log "
                       "WHERE station_id = ? AND rowid > ? ORDER BY rowid LIMIT ?",
                 (station_id, after, limit))
    return [{"id": rowid, "at": at, "context": json.loads(ctx), "decision": json.loads(dec)}
            for rowid, at, ctx, dec in rows]


def recent_latency_p95_ms(sqlite_path: str | Path, window_sec: float = 600.0) -> int | None:
    """최근 window_sec 동안 끝난 생성 작업의 LLM→L2→TTS 합계 지연 P95. 기록이 없으면 None."""
    path = Path(sqlite_path)
    if not path.exists():
        return None
    since = time.time() - window_sec
    rows = _read(path, "SELECT SUM(finished_at - started_at) FROM generation_log "
                       "WHERE stage IN ('llm', 'l2', 'tts') AND result = 'ok' "
                       "GROUP BY job_id HAVING MAX(finished_at) >= ?",
                 (since,))
    totals = sorted(r[0] for r in rows if r[0] is not None)
    if not totals:
        return None
    rank = max(0, -(-95 * len(totals) // 100) - 1)  # nearest-rank
    return round(totals[rank] * 1000)


def reflection_summary(sqlite_path: str | Path, station_id: str, q_max_sec: float = 120.0) -> dict:
    """반영 평가 지표 (CORNER_DESIGN 9.5절). 비율은 분모가 0이면 None."""
    path = Path(sqlite_path)
    if not path.exists():
        return {}
    arrived = dict(_read(path, "SELECT chat_type, COUNT(*) FROM input_log WHERE station_id = ? "
                               "GROUP BY chat_type", (station_id,)))
    reflected = dict(_read(path, "SELECT chat_type, COUNT(*) FROM reflection_log WHERE station_id = ? "
                                 "GROUP BY chat_type", (station_id,)))
    rows = _read(path, "SELECT quote_age_net, topic_match FROM reflection_log "
                       "WHERE station_id = ? AND chat_type = 'reaction'", (station_id,))

    def ratio(part: int, whole: int) -> float | None:
        return part / whole if whole else None

    return {
        "reaction_reflection_rate": ratio(reflected.get("reaction", 0), arrived.get("reaction", 0)),
        "standalone_reflection_rate": ratio(reflected.get("standalone", 0), arrived.get("standalone", 0)),
        "message_reflection_rate": ratio(reflected.get("message", 0), arrived.get("message", 0)),
        "timely_rate": ratio(sum(1 for net, _ in rows if net <= q_max_sec), len(rows)),
        "topic_match_rate": ratio(sum(1 for _, match in rows if match), len(rows)),
    }


class Telemetry:
    def __init__(self, sqlite_path: str | Path, station_id: str):
        path = Path(sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(_SCHEMA)
        # 기존 DB에는 CREATE IF NOT EXISTS가 새 컬럼을 만들지 않는다
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(input_log)")}
        for column in ("input_kind", "route"):
            if column not in columns:
                self._db.execute(f"ALTER TABLE input_log ADD COLUMN {column} TEXT")
        self._db.commit()
        self.station_id = station_id

    def log_stage(self, *, job_id: str, corner_type: str, request_ref: str | None,
                  stage: str, started_at: float, finished_at: float, result: str) -> None:
        self._db.execute(
            "INSERT INTO generation_log VALUES (?,?,?,?,?,?,?,?)",
            (job_id, self.station_id, corner_type, request_ref,
             stage, started_at, finished_at, result),
        )
        self._db.commit()

    def log_request_state(self, request_id: str, state) -> None:
        self._db.execute(
            "INSERT INTO request_log VALUES (?,?,?,?)",
            (request_id, self.station_id, str(state), time.time()),
        )
        self._db.commit()

    def log_decision(self, ctx, decision) -> None:
        # 대기 판단도 빠짐없이 기록한다 — 버퍼 판단과 인용 결정의 원천 (ENGINE_ARCHITECTURE 7장)
        self._db.execute(
            "INSERT INTO decision_log VALUES (?,?,?,?)",
            (self.station_id, time.time(), _dump(ctx), _dump(decision)),
        )
        self._db.commit()

    def log_corner(self, corner_type: str, label: str | None, event: str, produced_ms: int) -> None:
        """코너 진입(enter)·종료(exit)·건너뜀(skip)·대체(fallback) — 클락 수치 보정과 Q-03의 원천.

        produced_ms는 그 시점까지 제출한 오디오 누적 길이다. 코너의 실제 길이 = exit − enter.
        """
        self._db.execute(
            "INSERT INTO corner_log VALUES (?,?,?,?,?,?)",
            (self.station_id, time.time(), corner_type, label, event, produced_ms),
        )
        self._db.commit()

    def log_station(self, event: str, payload: dict) -> None:
        # station.created 원문 = 방송 시점의 persona 사본 (persona.md 9.4절). 분석은 이 사본을 기준으로 한다
        self._db.execute(
            "INSERT INTO station_log VALUES (?,?,?,?)",
            (self.station_id, time.time(), event, json.dumps(payload, ensure_ascii=False)),
        )
        self._db.commit()

    def log_input(self, req, anchor_kind: str | None, route: str = "pool") -> None:
        # chat_type은 채팅만 있다 — 유형별 반영률이 종류별로도 나오게 종류를 대신 넣는다
        self._db.execute(
            "INSERT INTO input_log (request_id, station_id, received_at, chat_type, rule, anchor_id, "
            "anchor_kind, input_kind, route) VALUES (?,?,?,?,?,?,?,?,?)",
            (req.request_id, self.station_id, req.received_at, str(req.chat_type or req.kind), req.rule,
             req.anchor_id, anchor_kind, req.kind, route),
        )
        self._db.commit()

    def log_selection(self, selection, anchor_id: str | None) -> None:
        self._db.execute(
            "INSERT INTO selection_log VALUES (?,?,?,?,?,?,?,?)",
            (self.station_id, time.time(), selection.situation, selection.criterion,
             selection.arrivals, anchor_id, json.dumps(selection.candidates, ensure_ascii=False),
             json.dumps([r.request_id for r in selection.picked])),
        )
        self._db.commit()

    def log_reflection(self, *, req, segment_id: str, started_at: float, quote_age_net: float,
                       topic_match: bool | None, script_chars: int) -> None:
        self._db.execute(
            "INSERT INTO reflection_log VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (req.request_id, self.station_id, segment_id, str(req.chat_type or req.kind), req.received_at,
             started_at, started_at - req.received_at, quote_age_net,
             None if topic_match is None else int(topic_match), script_chars, len(req.body)),
        )
        self._db.commit()

    def log_steer(self, *, request_id: str, segment_id: str, corner_type: str, rule: str,
                  old_topic: str | None, new_topic: str) -> None:
        """화제 전환 — 반영 ③단계(CORNER_DESIGN 9.4절). 실제 송출 여부는 reflection_log와 잇는다."""
        self._db.execute(
            "INSERT INTO steer_log VALUES (?,?,?,?,?,?,?,?)",
            (request_id, self.station_id, segment_id, time.time(), corner_type, rule,
             old_topic, new_topic),
        )
        self._db.commit()

    def log_aired(self, segment_id: str, at: float, script: str) -> None:
        self._db.execute("INSERT INTO aired_log VALUES (?,?,?,?)",
                         (self.station_id, segment_id, at, script))
        self._db.commit()

    def close(self) -> None:
        self._db.close()
