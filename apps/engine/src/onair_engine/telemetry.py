"""계측 — 상태 전이·생성 지연·정책 결정 기록 (설계 문서 7장).

실험 데이터(RQ1~RQ3)의 원천이므로 부가 기능이 아니라 항상 켜져 있는 기본 동작이다.
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
CREATE TABLE IF NOT EXISTS station_log (
    station_id TEXT, at REAL, event TEXT, payload_json TEXT
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


class Telemetry:
    def __init__(self, sqlite_path: str | Path, station_id: str):
        path = Path(sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(_SCHEMA)
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
        # ack 생략 결정도 빠짐없이 기록한다 — RQ2의 (b) 집단 관측의 원천
        self._db.execute(
            "INSERT INTO decision_log VALUES (?,?,?,?)",
            (self.station_id, time.time(), _dump(ctx), _dump(decision)),
        )
        self._db.commit()

    def log_station(self, event: str, payload: dict) -> None:
        # station.created 원문 = 방송 시점의 persona 사본 (persona.md 9.4절). 분석은 이 사본을 기준으로 한다
        self._db.execute(
            "INSERT INTO station_log VALUES (?,?,?,?)",
            (self.station_id, time.time(), event, json.dumps(payload, ensure_ascii=False)),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
