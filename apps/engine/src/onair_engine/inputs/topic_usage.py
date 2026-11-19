"""방송별로 쓴 갈래의 기록 — 방송이 달라져도 같은 이야기를 되풀이하지 않게 한다 (CORNER_DESIGN 5.9절).

같은 방송 안의 되풀이는 갈래 계획과 표현 세기(pipeline/repeats.py)가 막는다. 여기서는 방송 사이를 본다.
주제 토크가 갈래를 꺼낼 때마다 적어 두고, 다음 방송의 갈래 계획(inputs/topic_plan.py)이 규칙으로 거른다.
LLM의 판단에 맡기지 않는다.

- 기간(days) 안에 다른 방송이 쓴 데이터셋 이야깃거리는 검색에서 뺀다 (항목 id).
- 새로 짠 갈래도 기간 안에 다른 방송이 쓴 갈래와 임베딩 유사도가 threshold 이상이면 뺀다.

"다른 방송"은 스테이션 id로 가리지 않는다. 운영의 스테이션 id는 고정 채널이라(st_ch01…) 방송이 바뀌어도 같다
(BACKEND_MULTI_STATION 3절). 엔진이 방송마다 만든 id(broadcast)로 가린다. 엔진은 호스트가 누구인지 모르므로
호스트가 달라도 다른 방송으로 본다 — 이 엔진이 돌린 방송 전부가 대상이다.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..pipeline.embed import Embedder, EmbedError, Vector, dot

log = logging.getLogger(__name__)

DAYS = 30.0  # 이 기간이 지난 갈래는 다시 써도 된다 [임시값]
# 같은 이야기로 보는 유사도. 같은 주제의 다른 갈래는 남기고, 말만 바꾼 같은 갈래는 걸러야 한다.
# gemini-embedding-001로 갈래 글끼리 견주면 전혀 다른 주제도 0.79쯤 나오고 폭이 좁다 (2026-11-19 실측, T-017):
#   말만 다듬은 같은 갈래 0.89~0.97 (가운데 0.93), 같은 주제의 다른 갈래 가운데 0.87 · 상위 10% 0.91 · 상위 1% 0.94.
# 두 무리가 겹쳐서 깔끔히 가르는 값이 없다. 0.92면 같은 갈래 28개 중 17개를 잡고 다른 갈래 150쌍 중 8쌍을 잘못 뺀다.
# 데이터셋에서 온 갈래는 이 값과 무관하게 항목 id로 정확히 걸러진다 [초기값 — 다른 임베딩 모델을 쓰면 다시 잰다]
THRESHOLD = 0.92
# 주제와 이만큼 가까운 갈래만 "다른 방송에서 다룬 갈래"로 프롬프트에 알린다 — 이야깃거리 검색 기준과 같다 [초기값]
AVOID_MIN = 0.66

_SCHEMA = """
CREATE TABLE IF NOT EXISTS used_angles (
    broadcast TEXT, station_id TEXT, at REAL, topic TEXT, title TEXT, points_json TEXT, item_id TEXT,
    embedder TEXT, vec BLOB
);
CREATE INDEX IF NOT EXISTS used_angles_at ON used_angles (at);
"""


@dataclass
class Screened:
    kept: list
    reused: list  # 다른 방송과 겹쳐 뺀 갈래
    vectors: dict[str, Vector] = field(default_factory=dict)  # 갈래 제목 → 벡터. 꺼낼 때 기록에 함께 남긴다


class TopicUsage:
    def __init__(self, path: Path, embedder: Embedder, *, days: float = DAYS, threshold: float = THRESHOLD):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)
        self._embedder = embedder
        self.days = days
        self.threshold = threshold

    def close(self) -> None:
        self._db.close()

    def _since(self) -> float:
        return time.time() - self.days * 86400

    def record(self, broadcast: str, topic: str, angle, vector: Vector | None = None, *,
               station_id: str = "") -> None:
        """방송이 갈래를 꺼냈다. 기록이 실패해도 방송은 그대로 간다. station_id는 나중에 훑어볼 때만 쓴다."""
        try:
            self._db.execute(
                "INSERT INTO used_angles VALUES (?,?,?,?,?,?,?,?,?)",
                (broadcast, station_id, time.time(), topic, angle.title,
                 json.dumps(angle.points, ensure_ascii=False),
                 angle.source, self._embedder.name if vector is not None else None,
                 vector.tobytes() if vector is not None else None))
            self._db.commit()
        except sqlite3.Error as exc:
            log.warning("쓴 갈래를 기록하지 못했다: %s", exc)

    def used_items(self, broadcast: str) -> set[str]:
        """기간 안에 다른 방송이 쓴 데이터셋 이야깃거리의 id."""
        rows = self._db.execute(
            "SELECT DISTINCT item_id FROM used_angles WHERE at >= ? AND broadcast != ? AND item_id IS NOT NULL",
            (self._since(), broadcast))
        return {item_id for (item_id,) in rows}

    def _recent(self, broadcast: str) -> list[tuple[str, Vector]]:
        """기간 안에 다른 방송이 쓴 갈래의 (제목, 벡터). 다른 임베딩 모델로 만든 벡터는 견줄 수 없어 뺀다."""
        rows = self._db.execute(
            "SELECT title, vec FROM used_angles WHERE at >= ? AND broadcast != ? AND embedder = ?",
            (self._since(), broadcast, self._embedder.name))
        recent = []
        for title, blob in rows:
            vector = Vector("f")
            vector.frombytes(blob)
            recent.append((title, vector))
        return recent

    async def nearby_titles(self, broadcast: str, topic: str, limit: int = 10) -> list[str]:
        """다른 방송이 쓴 갈래 가운데 오늘 주제와 가까운 것의 제목 — 갈래를 짤 때 피하라고 알려 준다.

        거르는 일은 screen()이 한다. 이것은 걸러질 갈래를 처음부터 덜 짜게 하려는 것이다.
        """
        recent = self._recent(broadcast)
        if not recent:
            return []
        try:
            (query,) = await self._embedder.embed([topic], query=True)
        except EmbedError as exc:
            log.warning("다른 방송의 갈래를 찾지 못했다: %s", exc)
            return []
        scored = sorted(((dot(query, vector), title) for title, vector in recent), reverse=True)
        return list(dict.fromkeys(title for score, title in scored if score >= AVOID_MIN))[:limit]

    async def screen(self, broadcast: str, angles: list, *, keep_min: int = 0) -> Screened:
        """다른 방송이 쓴 갈래와 겹치는 것을 뺀다.

        다 빼면 주제 한 줄로만 말하게 되어 방송 안의 되풀이가 심해진다. 그래서 keep_min개는 남긴다 —
        겹친 것 가운데 덜 비슷한 것부터 되살린다.
        """
        if not angles:
            return Screened([], [])
        try:
            vectors = await self._embedder.embed([angle.text() for angle in angles])
        except EmbedError as exc:  # 견줄 수 없으면 거르지 않는다
            log.warning("갈래를 다른 방송과 견주지 못했다: %s", exc)
            return Screened(list(angles), [])
        by_title = {angle.title: vector for angle, vector in zip(angles, vectors, strict=True)}
        recent = self._recent(broadcast)
        used = self.used_items(broadcast)
        worst = {angle.title: max((dot(by_title[angle.title], vector) for _, vector in recent), default=0.0)
                 for angle in angles}
        reused = [a for a in angles if a.source in used or worst[a.title] >= self.threshold]
        reused.sort(key=lambda a: (a.source in used, worst[a.title]))
        while reused and len(angles) - len(reused) < keep_min:
            reused.pop(0)
        dropped = {id(a) for a in reused}
        return Screened([a for a in angles if id(a) not in dropped], reused, by_title)
