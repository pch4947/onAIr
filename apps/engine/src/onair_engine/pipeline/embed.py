"""임베딩 어댑터 — dummy(글자 묶음 해시), gemini(Gemini API batchEmbedContents).

이야깃거리 검색(inputs/talk_bank.py)과 방송 간 재사용 검사(inputs/topic_usage.py)가 쓴다.
llm.py와 같이 표준 라이브러리 REST만 쓰고, 블로킹 urlopen은 to_thread로 감싼다.
벡터는 길이 1로 맞춰 돌려준다 — 두 벡터의 내적(dot)이 곧 코사인 유사도다.
"""
from __future__ import annotations

import asyncio
import math
import operator
import os
import zlib
from array import array
from typing import Protocol

from .llm import LlmError, _post_json

Vector = array  # array('f') — numpy 없이 sqlite BLOB으로 그대로 넣고 꺼낸다

DEFAULT_GEMINI_EMBED_MODEL = "gemini-embedding-001"  # 글만 넣는다 — taskType을 받는 모델이다
GEMINI_DIM = 768  # 기본은 3072. 수천 건을 전수 비교하므로 줄인다. 줄이면 길이를 직접 1로 맞춰야 한다
GEMINI_BATCH = 100  # batchEmbedContents 한 번에 받는 최대 건수 (넘기면 400)


class EmbedError(RuntimeError):
    """임베딩 호출 실패 — 부르는 쪽은 검색 없이 진행한다."""


class Embedder(Protocol):
    name: str  # 벡터 캐시의 열쇠 — 모델이나 차원이 바뀌면 벡터를 다시 만든다

    async def embed(self, texts: list[str], *, query: bool = False) -> list[Vector]:
        """글마다 길이 1인 벡터. query는 찾는 쪽 글(주제)일 때 — 찾아질 글(이야깃거리)과 구분한다."""
        ...


def unit(values) -> Vector:
    norm = math.sqrt(sum(x * x for x in values))
    return array("f", (x / norm for x in values) if norm else values)


def dot(a: Vector, b: Vector) -> float:
    return sum(map(operator.mul, a, b))


class DummyEmbedder:
    """글자 2~3개 묶음을 해시해 만든 벡터 — API 키 없이 검색 흐름을 돌려 본다.

    뜻이 아니라 글자가 겹치는 만큼 가깝다. 같은 글은 언제나 같은 벡터다 (hash()와 달리 실행마다 같다).
    """

    name = "dummy-256"
    DIM = 256

    async def embed(self, texts: list[str], *, query: bool = False) -> list[Vector]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> Vector:
        chars = "".join(text.split())
        values = [0.0] * self.DIM
        for n in (2, 3):
            for i in range(len(chars) - n + 1):
                values[zlib.crc32(chars[i:i + n].encode("utf-8")) % self.DIM] += 1.0
        return unit(values)


class GeminiEmbedder:
    """Gemini API batchEmbedContents. 인증은 GEMINI_API_KEY를 x-goog-api-key 헤더로 보낸다."""

    ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"

    def __init__(self, model: str = DEFAULT_GEMINI_EMBED_MODEL, *, api_key: str | None = None,
                 dim: int = GEMINI_DIM, endpoint: str = ENDPOINT, timeout: float = 15.0, retries: int = 1):
        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("gemini 임베딩을 쓰려면 GEMINI_API_KEY가 필요합니다 (apps/engine/.env 또는 환경변수)")
        self._api_key = api_key
        self.model = model
        self.dim = dim
        self.name = f"{model}-{dim}"
        self._endpoint = endpoint.format(model=model)
        self._timeout = timeout
        self.retries = retries

    async def embed(self, texts: list[str], *, query: bool = False) -> list[Vector]:
        task = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
        vectors: list[Vector] = []
        for start in range(0, len(texts), GEMINI_BATCH):
            body = {"requests": [
                {"model": f"models/{self.model}", "content": {"parts": [{"text": text}]},
                 "taskType": task, "outputDimensionality": self.dim}
                for text in texts[start:start + GEMINI_BATCH]]}
            data = await self._post(body)
            try:
                batch = [unit(item["values"]) for item in data["embeddings"]]
            except (KeyError, TypeError) as exc:
                raise EmbedError(f"gemini 임베딩 응답 형식 오류: {str(data)[:200]}") from exc
            if len(batch) != len(body["requests"]):
                raise EmbedError(f"gemini 임베딩이 {len(body['requests'])}건 중 {len(batch)}건만 돌려줬습니다")
            vectors += batch
        return vectors

    async def _post(self, body: dict) -> dict:
        """한 번은 다시 부른다 — 가끔 응답이 한참 늦는다 (2026-11-19 실측: 평소 2초인 호출이 60초를 넘김)."""
        for attempt in range(1 + self.retries):
            try:
                return await asyncio.to_thread(_post_json, self._endpoint, body,
                                               {"x-goog-api-key": self._api_key}, self._timeout, "gemini 임베딩")
            except LlmError as exc:
                if attempt == self.retries:
                    raise EmbedError(str(exc)) from exc
        raise AssertionError("unreachable")


def make_embedder(kind: str) -> Embedder:
    if kind == "dummy":
        return DummyEmbedder()
    if kind == "gemini":
        return GeminiEmbedder()
    raise ValueError(f"unknown embedder: {kind}")
