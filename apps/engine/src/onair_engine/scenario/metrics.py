"""회차 지표 목록 — 요약 표(summary.md), 비교(compare.py), 장부(ledger.jsonl)가 같은 정의를 쓴다.

지표마다 좋아지는 방향을 적는다. 방향이 없는 지표(말 비율, 화제 전환 수)는 비교 표에 참고로만 나온다 —
어느 쪽이 좋은지는 사람이 정한다.
"""
from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass

LOW, HIGH = "low", "high"


@dataclass(frozen=True)
class Metric:
    key: str  # 장부·compare.json에 쓰는 이름
    label: str  # 표 머리
    get: Callable[[dict], float | int | None]  # report.json → 값
    better: str | None = None  # LOW | HIGH | None(방향 없음)
    summary: bool = True  # summary.md 표에 넣는다


def _path(*keys: str) -> Callable[[dict], float | int | None]:
    def get(report: dict):
        value = report
        for key in keys:
            if not isinstance(value, dict):
                return None
            value = value.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return get


def _expect_pass_rate(report: dict) -> float | None:
    expect = report.get("expect") or []
    return round(sum(e["passed"] for e in expect) / len(expect), 3) if expect else None


METRICS: list[Metric] = [
    Metric("jobs", "생성", _path("generation", "jobs")),
    Metric("fallback", "대체 멘트", _path("generation", "outcomes", "fallback"), LOW),
    Metric("silence_total_sec", "무음 합계(초)", _path("sim", "silence_total_sec"), LOW),
    Metric("silence_max_sec", "최대 무음(초)", _path("sim", "silence_max_sec"), LOW),
    Metric("reaction_reflection_rate", "반응형 반영률", _path("reflection", "reaction_reflection_rate"), HIGH),
    Metric("standalone_reflection_rate", "독립형 반영률",
           _path("reflection", "standalone_reflection_rate"), HIGH),
    Metric("message_reflection_rate", "문자 반영률", _path("reflection", "message_reflection_rate"), HIGH),
    Metric("timely_rate", "제때 반영", _path("reflection", "timely_rate"), HIGH, summary=False),
    Metric("age_net_p50", "반영 시간 중앙값(초)", _path("reflection", "age_net_sec", "p50"), LOW),
    Metric("quote_missing_rate", "인용 누락률", _path("generation", "quote_missing_rate"), LOW),
    Metric("corner_fallback_rate", "코너 대체율", _path("corner_rates", "fallback_rate"), LOW),
    Metric("job_sec_p95", "세그먼트 생성 P95(초)", _path("generation", "job_sec", "p95"), LOW),
    Metric("steer", "화제 전환", _path("steer")),
    Metric("talk_ratio", "말 비율", _path("sim", "talk_ratio")),
    Metric("talk_between_songs_p50", "곡 사이 토크(초)", _path("sim", "talk_between_songs_p50")),
    Metric("back_to_back_songs", "곡 연속", _path("sim", "back_to_back_songs"), LOW),
    Metric("same_opening_rate", "같은 첫머리", _path("scripts", "same_opening_rate"), LOW),
    Metric("similarity_mean", "직전 멘트 유사도", _path("scripts", "similarity_mean"), LOW, summary=False),
    Metric("topic_intros", "주제 소개", _path("scripts", "topic_intros")),
    Metric("expect_pass_rate", "기대 결과 통과율", _expect_pass_rate, HIGH, summary=False),
    # 실행기 자체의 상태 — 크면 동시 실행이 타이밍 지표를 틀었다 (sim.LoopLag). 엔진 성능이 아니다
    Metric("loop_lag_ms_p95", "루프 지연 P95(ms)", _path("sim", "loop_lag_ms_p95")),
]


def spread(values: list[float]) -> dict | None:
    """평균과 표본 표준편차. 값이 없으면 None, 하나면 sd는 None이다."""
    if not values:
        return None
    return {"n": len(values), "mean": round(statistics.fmean(values), 4),
            "sd": round(statistics.stdev(values), 4) if len(values) > 1 else None,
            "min": min(values), "max": max(values)}


def aggregate(reports: list[dict]) -> dict[str, dict]:
    """회차 여러 개의 지표별 평균·표준편차 — 장부 한 줄의 metrics."""
    out = {}
    for m in METRICS:
        stats = spread([v for v in (m.get(r) for r in reports) if v is not None])
        if stats is not None:
            out[m.key] = stats
    return out
