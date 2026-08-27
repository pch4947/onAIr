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
"""


def _dump(obj) -> str:
    return json.dumps(dataclasses.asdict(obj), ensure_ascii=False, default=str)


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

    def close(self) -> None:
        self._db.close()
