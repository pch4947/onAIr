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
CREATE TABLE IF NOT EXISTS aggregate_log (
    station_id TEXT, segment_id TEXT, at REAL, kind TEXT, corner_type TEXT, keyword TEXT,
    count INTEGER, total INTEGER
);
CREATE TABLE IF NOT EXISTS story_judge_log (
    story_id TEXT, station_id TEXT, at REAL, relevant INTEGER, method TEXT
);
CREATE TABLE IF NOT EXISTS topic_plan_log (
    station_id TEXT, at REAL, method TEXT, dropped INTEGER, angles_json TEXT
);
CREATE TABLE IF NOT EXISTS aired_log (
    station_id TEXT, segment_id TEXT, at REAL, script TEXT
);
CREATE TABLE IF NOT EXISTS event_log (
    station_id TEXT, at REAL, kind TEXT, summary TEXT, data_json TEXT
);
"""

# 이벤트 요약 문구 — 엔진 패널에 그대로 보인다 (계약 6.1절 events)
_STAGES = {"llm": "LLM", "l1": "LLM 거부", "quote": "인용", "l2": "안전 검사", "tts": "TTS",
           "fallback": "대체 멘트로 전환"}
_KINDS = {"chat": "채팅", "story": "채팅", "mood": "채팅", "message": "문자", "song_request": "신청곡",
          "board_story": "게시판 사연"}
_ROUTES = {"pool": "채팅 후보", "inbox": "문자 받은편지함", "song_request": "곡 대기",
           "story_pool": "사연 후보", "not_on_air": "방송 미반영"}
_CORNER_EVENTS = {"enter": "코너 진입", "exit": "코너 종료", "skip": "코너 건너뜀",
                  "fallback": "코너 대체", "length": "코너 멘트 길이",
                  "hold": "곡 건너뜀"}
_CRITERIA = {"recency": "최신순", "relevance": "주제 관련", "diversity": "다양성"}
_EXCERPT = 40


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


def read_events(sqlite_path: str | Path, station_id: str, after: int = 0,
                limit: int = 100) -> list[dict]:
    """엔진 이벤트 페이지 — 엔진 패널용. rowid가 커서다 (계약 6.1절)."""
    path = Path(sqlite_path)
    if not path.exists():
        return []
    rows = _read(path, "SELECT rowid, at, kind, summary, data_json FROM event_log "
                       "WHERE station_id = ? AND rowid > ? ORDER BY rowid LIMIT ?",
                 (station_id, after, limit))
    return [{"id": rowid, "at": at, "kind": kind, "summary": summary, "data": json.loads(data)}
            for rowid, at, kind, summary, data in rows]


def _stage_text(s: dict) -> str:
    name = _STAGES.get(s["stage"], s["stage"])
    if s["result"] == "error":
        return f"{name} 실패"
    if s["stage"] == "l2":
        return "안전 검사 통과" if s["result"] == "ok" else "안전 검사 차단"
    if s["stage"] in ("l1", "fallback"):
        return name
    if s["stage"] == "quote":
        return "인용 확인" if s["result"] == "ok" else "인용 누락"
    return f"{name} {s['elapsed_sec']:.1f}초"


def _excerpt(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _EXCERPT else text[:_EXCERPT] + "…"


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
        self.corner_titles: dict[str, str] = {}  # 패널 요약에 쓸 코너 이름 — 엔진이 형식에서 채운다
        self._jobs: dict[str, list[dict]] = {}  # 끝나지 않은 생성 작업의 단계 기록

    def _event(self, kind: str, summary: str, /, **data) -> None:
        """엔진 패널용 이벤트 — 다른 기록과 같은 트랜잭션으로 넣는다. 내부 ID(requester_ref)는 넣지 않는다."""
        self._db.execute("INSERT INTO event_log VALUES (?,?,?,?,?)",
                         (self.station_id, time.time(), kind, summary,
                          json.dumps(data, ensure_ascii=False, default=str)))

    def log_stage(self, *, job_id: str, corner_type: str, request_ref: str | None,
                  stage: str, started_at: float, finished_at: float, result: str) -> None:
        self._db.execute(
            "INSERT INTO generation_log VALUES (?,?,?,?,?,?,?,?)",
            (job_id, self.station_id, corner_type, request_ref,
             stage, started_at, finished_at, result),
        )
        # 패널에는 세그먼트 하나를 한 줄로 — 동시 생성의 단계들이 섞이지 않게 끝날 때 모아서 남긴다
        stages = self._jobs.setdefault(job_id, [])
        stages.append({"stage": stage, "result": result, "elapsed_sec": round(finished_at - started_at, 3)})
        if (stage in ("tts", "fallback") and result != "error") or result == "reject":
            del self._jobs[job_id]
            parts = [_stage_text(s) for s in stages]
            title = self.corner_titles.get(corner_type, corner_type)
            self._event("generation", f"생성 · {title} · " + " · ".join(parts),
                        job_id=job_id, corner_type=corner_type, request_ref=request_ref, stages=stages)
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

    def log_corner(self, corner_type: str, label: str | None, event: str, produced_ms: int,
                   title: str | None = None) -> None:
        """코너 진입(enter)·종료(exit)·건너뜀(skip)·대체(fallback)·멘트 길이(length) — 클락 수치 보정과 Q-03의 원천.

        produced_ms는 그 시점까지 제출한 오디오 누적 길이다. length만은 그 코너가 낸 멘트 길이다 —
        코너의 생성이 다 끝났을 때 남긴다. exit − enter에는 앞 코너에서 소개한 곡이 섞인다 (#220).
        """
        self._db.execute(
            "INSERT INTO corner_log VALUES (?,?,?,?,?,?)",
            (self.station_id, time.time(), corner_type, label, event, produced_ms),
        )
        name = " · ".join(p for p in (title or corner_type, label) if p)
        reason = {"skip": " (소재 없음)", "fallback": " (소재 없음)",
                  "hold": " (곡 사이에 다른 코너가 없었음)"}.get(event, "")
        if event == "length":
            reason = f" ({produced_ms / 1000:.0f}초)"
        self._event("corner", f"{_CORNER_EVENTS.get(event, event)} · {name}{reason}",
                    corner_type=corner_type, label=label, event=event, title=title)
        self._db.commit()

    def log_corner_request(self, corner_type: str, reason: str | None = None) -> None:
        """코너 변경 요청(corner.skip)과 그 처리 — 패널에만 남긴다. reason이 있으면 받아들이지 않았다."""
        name = self.corner_titles.get(corner_type, corner_type)
        summary = f"코너 변경 무시 · {name} ({reason})" if reason else f"코너 변경 요청 · {name}"
        self._event("corner", summary, corner_type=corner_type,
                    event="request_ignored" if reason else "request", reason=reason)
        self._db.commit()

    def log_discarded(self, segments: int, returned: int) -> None:
        """백엔드가 재생하지 않고 버린 멘트(segment.discarded) — 패널에만 남긴다."""
        summary = f"미송출 멘트 {segments}개 버림" + (f" · 입력 {returned}건 되돌림" if returned else "")
        self._event("corner", summary, event="discarded", segments=segments, returned=returned)
        self._db.commit()

    def log_station(self, event: str, payload: dict) -> None:
        # station.created 원문 = 방송 시점의 persona 사본 (persona.md 9.4절). 분석은 이 사본을 기준으로 한다
        self._db.execute(
            "INSERT INTO station_log VALUES (?,?,?,?)",
            (self.station_id, time.time(), event, json.dumps(payload, ensure_ascii=False)),
        )
        if event == "station.started":
            self._event("station", f"방송 시작 · 주제 {payload.get('topic')}", event=event,
                        topic=payload.get("topic"))
        elif event != "station.created":  # 생성 원문은 persona 전체라 패널에 싣지 않는다
            self._event("station", event, event=event)
        self._db.commit()

    def log_input(self, req, anchor_kind: str | None, route: str = "pool") -> None:
        # chat_type은 채팅만 있다 — 유형별 반영률이 종류별로도 나오게 종류를 대신 넣는다
        self._db.execute(
            "INSERT INTO input_log (request_id, station_id, received_at, chat_type, rule, anchor_id, "
            "anchor_kind, input_kind, route) VALUES (?,?,?,?,?,?,?,?,?)",
            (req.request_id, self.station_id, req.received_at, str(req.chat_type or req.kind), req.rule,
             req.anchor_id, anchor_kind, req.kind, route),
        )
        self._event("input", f"입력 라우팅 · {_KINDS.get(req.kind, req.kind)} → {_ROUTES.get(route, route)}",
                    request_id=req.request_id, kind=req.kind, route=route,
                    chat_type=str(req.chat_type) if req.chat_type else None, rule=req.rule,
                    body=_excerpt(req.body or ""))
        self._db.commit()

    def log_selection(self, selection, anchor_id: str | None) -> None:
        self._db.execute(
            "INSERT INTO selection_log VALUES (?,?,?,?,?,?,?,?)",
            (self.station_id, time.time(), selection.situation, selection.criterion,
             selection.arrivals, anchor_id, json.dumps(selection.candidates, ensure_ascii=False),
             json.dumps([r.request_id for r in selection.picked])),
        )
        if selection.candidates:  # 후보가 없을 때마다 남기면 패널이 빈 선별로 덮인다
            picked = ", ".join(f'"{_excerpt(r.body)}"' for r in selection.picked) or "없음"
            criterion = _CRITERIA.get(selection.criterion, selection.criterion)
            self._event("selection",
                        f"채팅 선별 · 후보 {len(selection.candidates)}건 중 {picked} 선택 ({criterion})",
                        situation=selection.situation, criterion=selection.criterion,
                        candidates=len(selection.candidates),
                        picked=[r.request_id for r in selection.picked])
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
        self._event("steer", f"화제 전환 · {old_topic} → {new_topic}", request_id=request_id,
                    segment_id=segment_id, rule=rule)
        self._db.commit()

    def log_aggregate(self, *, segment_id: str, kind: str, corner_type: str, keyword: str | None,
                      count: int, total: int | None) -> None:
        """집계 언급·예고 — 개인 반영으로 세지 않는다 (CORNER_DESIGN 9.2절, RQ-A 보조 조건)."""
        self._db.execute(
            "INSERT INTO aggregate_log VALUES (?,?,?,?,?,?,?,?)",
            (self.station_id, segment_id, time.time(), kind, corner_type, keyword, count, total),
        )
        if kind == "aggregate":
            summary = f"집계 언급 · \"{keyword}\" {count}명 / 전체 {total}명"
        else:
            summary = f"문자 코너 예고 · 받은편지함 {count}건"
        self._event("aggregate", summary, kind=kind, segment_id=segment_id, keyword=keyword,
                    count=count, total=total)
        self._db.commit()

    def log_story_judgement(self, story_id: str, relevant: bool, method: str) -> None:
        """게시판 사연 주제 관련도 — method는 llm 또는 keyword(LLM 실패 시 대체) (CORNER_DESIGN 5.4절)."""
        self._db.execute("INSERT INTO story_judge_log VALUES (?,?,?,?,?)",
                         (story_id, self.station_id, time.time(), int(relevant), method))
        verdict = "관련 있음" if relevant else "관련 없음 → 후보 제외"
        self._event("story_judge", f"사연 판정 · {verdict} ({method})", story_id=story_id,
                    relevant=relevant, method=method)
        self._db.commit()

    def log_topic_plan(self, angles: list, method: str, dropped: int = 0) -> None:
        """이야기 갈래 계획 — method는 llm 또는 failed(주제 한 줄로 진행). dropped는 L2에 걸려 뺀 갈래 수."""
        self._db.execute("INSERT INTO topic_plan_log VALUES (?,?,?,?,?)",
                         (self.station_id, time.time(), method, dropped,
                          json.dumps([dataclasses.asdict(a) for a in angles], ensure_ascii=False)))
        if method == "llm":
            summary = f"이야기 갈래 {len(angles)}개 준비" + (f" (안전 검사로 {dropped}개 제외)" if dropped else "")
        else:
            summary = "이야기 갈래 생성 실패 → 주제만으로 진행"
        self._event("topic_plan", summary, method=method, angles=[a.title for a in angles],
                    dropped=dropped)
        self._db.commit()

    def log_angle(self, angle, index: int, total: int) -> None:
        """주제 토크가 이번 진입에 쓸 갈래를 꺼냈다 — 패널에만 남긴다. 계획 전체는 topic_plan_log에 있다."""
        self._event("topic_plan", f"이야기 갈래 {index}/{total} · {angle.title}", title=angle.title,
                    index=index, total=total)
        self._db.commit()

    def log_callback(self, aired) -> None:
        """청취자 이야기 다시 꺼내기 — 패널에만 남긴다. 소재 출처는 decision_log의 material에 있다."""
        self._event("callback", f"다시 꺼내기 · \"{_excerpt(aired.body)}\"", kind=aired.kind)
        self._db.commit()

    def log_track(self, track: dict) -> None:
        """다음 곡(track.queued) — 패널에만 남긴다. 곡 기록의 원본은 백엔드에 있다."""
        title = " - ".join(str(track[k]) for k in ("title", "artist") if track.get(k))
        requested = bool(track.get("request_id"))
        self._event("track", f"{'신청곡 도착' if requested else '다음 곡'} · {title}",
                    track_ref=track.get("track_ref"), request_id=track.get("request_id"),
                    with_comment=bool(track.get("requester_comment")))
        self._db.commit()

    def log_rss(self, counts: dict[str, int], failed: int) -> None:
        """기사 수집 결과 — 분야별 캐시 기사 수. 실패한 피드는 직전 결과를 유지한다."""
        parts = ", ".join(f"{section} {n}건" for section, n in counts.items()) or "기사 없음"
        self._event("rss", f"기사 수집 · {parts}" + (f" (피드 {failed}개 실패)" if failed else ""),
                    counts=counts, failed=failed)
        self._db.commit()

    def log_aired(self, segment_id: str, at: float, script: str) -> None:
        self._db.execute("INSERT INTO aired_log VALUES (?,?,?,?)",
                         (self.station_id, segment_id, at, script))
        self._event("aired", f"송출 · {_excerpt(script)}", segment_id=segment_id)
        self._db.commit()

    def close(self) -> None:
        self._db.close()
