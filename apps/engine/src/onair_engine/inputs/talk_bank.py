"""이야깃거리 데이터셋과 검색 — 갈래 계획이 빈손으로 시작하지 않게 한다 (CORNER_DESIGN 5.9절).

외부 글을 긁지 않는다. LLM으로 미리 만들어 둔 이야깃거리(config/talk_bank, 만드는 도구는 onair-bank)에서
오늘 주제와 뜻이 가까운 것을 임베딩으로 찾는다 (RAG). 찾은 것은 갈래 계획 프롬프트의 참고 자료가 된다
(inputs/topic_plan.py).

- 데이터셋은 형식(talk·music)과 페르소나(프리셋 + 공통)별로 나뉜다. 같은 주제라도 심야 공부 방송과 출근길
  방송은 꺼낼 장면이 다르다.
- 호스트의 페르소나는 자유 입력이라 프리셋과 이름으로 맞출 수 없다. 컨셉과 말투를 임베딩해 가장 닮은 프리셋
  하나를 고르고, 그 프리셋의 이야깃거리와 공통 이야깃거리에서 찾는다.
- 벡터 DB는 쓰지 않는다. 수천 건이라 전수 비교로 충분하다. 벡터는 sqlite 파일에 캐시하고, 캐시에 없는
  것만 임베딩한다 — 데이터셋을 고치면 고친 항목만 다시 만든다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..pipeline.embed import GEMINI_BATCH, Embedder, Vector, dot

log = logging.getLogger(__name__)

COMMON = "common"  # 어느 페르소나에나 쓰는 이야깃거리
DEFAULT_FORMAT = "talk"  # 이야깃거리가 없는 형식(news)의 주제 토크는 토크 형식 것을 쓴다
ITEMS_DIR = "items"
PERSONAS_FILE = "personas.yaml"
# 주제와의 유사도가 이보다 낮으면 참고 자료로 넣지 않는다 — 동떨어진 자료는 갈래를 주제 밖으로 끈다.
# gemini-embedding-001(768차원)은 점수가 좁게 몰린다: 데이터셋에 있는 주제는 0.69~0.79, 없는 주제("우주 탐사")는
# 가장 가까운 것도 0.60이다 (2026-11-19 실측, ENGINE_TUNING_LOG T-017). 다른 임베딩 모델을 쓰면 다시 잰다 [초기값]
MIN_SCORE = 0.66
# 유사도가 비슷하면 닮은 프리셋의 이야깃거리를 공통보다 앞에 둔다. 점수 폭이 좁아 조금만 준다 —
# 0.02일 때는 주제에 덜 맞는 프리셋 것이 잘 맞는 공통 것을 밀어냈다 [초기값]
PERSONA_BONUS = 0.01


@dataclass(frozen=True)
class BankItem:
    id: str
    theme: str  # 만들 때 준 큰 주제 — 검색 열쇠가 아니라 사람이 훑어볼 때 쓴다
    title: str
    points: tuple[str, ...]
    format: str
    persona: str

    def text(self) -> str:
        """임베딩할 글 — 큰 주제까지 넣어야 짧은 제목만으로 엉뚱한 주제에 걸리지 않는다."""
        return "\n".join([f"{self.theme} — {self.title}", *self.points])


@dataclass(frozen=True)
class Hit:
    item: BankItem
    score: float  # 주제와의 코사인 유사도 (보너스 제외)


@dataclass(frozen=True)
class Found:
    hits: tuple[Hit, ...]
    persona: str | None  # 가장 닮은 프리셋
    skipped: int  # 주제에 맞지만 다른 방송이 이미 써서 뺀 수


def load_items(directory: Path) -> list[BankItem]:
    items = []
    for path in sorted((directory / ITEMS_DIR).glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                raw = json.loads(line)
                items.append(BankItem(**{**raw, "points": tuple(raw["points"])}))
    return items


def load_personas(directory: Path) -> dict[str, str]:
    """프리셋 id → 컨셉과 말투를 적은 한 줄. 호스트의 페르소나와 견줄 때 쓴다."""
    path = directory / PERSONAS_FILE
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def persona_text(concept: str, tone: str) -> str:
    """프리셋과 호스트의 페르소나를 같은 모양의 글로 만든다 — 모양이 같아야 유사도를 견줄 수 있다."""
    return f"{concept}. {tone}"


class _VectorCache:
    """글 → 벡터. 열쇠에 임베딩 모델 이름이 들어가 모델을 바꾸면 저절로 다시 만든다."""

    def __init__(self, path: Path | None, embedder_name: str):
        self._name = embedder_name
        self._db = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path)
            self._db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vec BLOB)")

    def key(self, text: str) -> str:
        return hashlib.sha1(f"{self._name}\n{text}".encode()).hexdigest()

    def get(self, texts: list[str]) -> dict[str, Vector]:
        if self._db is None:
            return {}
        by_key = {self.key(text): text for text in texts}
        found = {}
        for key, blob in self._db.execute("SELECT key, vec FROM vectors"):
            if key in by_key:
                vector = Vector("f")
                vector.frombytes(blob)
                found[by_key[key]] = vector
        return found

    def put(self, vectors: dict[str, Vector]) -> None:
        if self._db is None:
            return
        self._db.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?)",
                             [(self.key(text), vector.tobytes()) for text, vector in vectors.items()])
        self._db.commit()


class TalkBank:
    def __init__(self, items: list[BankItem], personas: dict[str, str], embedder: Embedder, *,
                 cache_path: Path | None = None, min_score: float = MIN_SCORE,
                 persona_bonus: float = PERSONA_BONUS):
        self.items = items
        self.personas = personas
        self.min_score = min_score
        self.persona_bonus = persona_bonus
        self._embedder = embedder
        self._cache = _VectorCache(cache_path, embedder.name)
        self._vectors: dict[str, Vector] = {}  # 글 → 벡터 (이야깃거리와 프리셋 설명)
        self._formats = {item.format for item in items}
        self._lock = asyncio.Lock()

    @property
    def indexed(self) -> bool:
        return bool(self._vectors)

    async def index(self) -> int:
        """벡터를 준비한다. 캐시에 없는 것만 임베딩하고 그 수를 돌려준다. 실패하면 EmbedError.

        한 묶음씩 저장한다 — 도중에 끊겨도 다음에 이어서 만든다.
        """
        async with self._lock:
            if self.indexed:
                return 0
            texts = list(dict.fromkeys([*(item.text() for item in self.items), *self.personas.values()]))
            vectors = self._cache.get(texts)
            missing = [text for text in texts if text not in vectors]
            for start in range(0, len(missing), GEMINI_BATCH):
                chunk = missing[start:start + GEMINI_BATCH]
                made = dict(zip(chunk, await self._embedder.embed(chunk), strict=True))
                self._cache.put(made)
                vectors.update(made)
            self._vectors = vectors
            if missing:
                log.info("이야깃거리 벡터 %d건을 새로 만들었다 (전체 %d건)", len(missing), len(texts))
            return len(missing)

    async def search(self, topic: str, *, fmt: str, persona: str, k: int,
                     exclude: frozenset[str] | set[str] = frozenset()) -> Found:
        """주제와 가까운 이야깃거리 k개까지 — 가까운 순. persona는 호스트 페르소나의 persona_text()."""
        await self.index()
        (query,) = await self._embedder.embed([topic], query=True)
        nearest = await self._nearest_persona(persona)
        fmt = fmt if fmt in self._formats else DEFAULT_FORMAT
        ranked: list[tuple[float, Hit]] = []
        skipped = 0
        for item in self.items:
            if item.format != fmt or item.persona not in (nearest, COMMON):
                continue
            score = dot(query, self._vectors[item.text()])
            if score < self.min_score:
                continue
            if item.id in exclude:
                skipped += 1
                continue
            ranked.append((score + (self.persona_bonus if item.persona == nearest else 0.0), Hit(item, score)))
        ranked.sort(key=lambda pair: -pair[0])
        return Found(tuple(hit for _, hit in ranked[:k]), nearest, skipped)

    async def _nearest_persona(self, text: str) -> str | None:
        if not self.personas or not text.strip():
            return None
        (vector,) = await self._embedder.embed([text])
        return max(self.personas, key=lambda pid: dot(vector, self._vectors[self.personas[pid]]))


_SHARED: dict[tuple[str, str], TalkBank] = {}


def shared_bank(directory: Path, embedder: Embedder, cache_path: Path | None) -> TalkBank:
    """엔진 프로세스에 하나 — 방송마다 데이터셋을 다시 읽고 벡터를 다시 올리지 않는다."""
    key = (str(directory.resolve()), embedder.name)
    if key not in _SHARED:
        _SHARED[key] = TalkBank(load_items(directory), load_personas(directory), embedder, cache_path=cache_path)
    return _SHARED[key]
