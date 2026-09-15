"""백엔드 어댑터 — 설계 문서 6장.

통신 채널이 미확정(확인 1)이므로 stdout 더미가 기본이고,
Redis Streams 어댑터는 1주차 계약 확정 후 구현한다.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator
from typing import Protocol

from .domain import RequestState, SegmentSubmission


class Transport(Protocol):
    async def publish_segment(self, sub: SegmentSubmission) -> None: ...

    async def notify_request_state(self, request_id: str, state: RequestState) -> None: ...

    def events(self) -> AsyncIterator[dict]:
        """백엔드 발행 이벤트 스트림 (request.arrived, backpressure, station.* 등)."""
        ...


class StdoutTransport:
    """M0 더미: 제출/통보를 JSON 라인으로 출력한다. 이벤트는 발생하지 않는다."""

    async def publish_segment(self, sub: SegmentSubmission) -> None:
        payload = json.dumps(dataclasses.asdict(sub), ensure_ascii=False, default=str)
        print("SUBMIT " + payload, flush=True)

    async def notify_request_state(self, request_id: str, state: RequestState) -> None:
        print(f"REQUEST_STATE {request_id} -> {state}", flush=True)

    async def events(self) -> AsyncIterator[dict]:
        while True:
            await asyncio.sleep(3600)
            yield {}


class RedisTransport:
    """Redis Streams 어댑터 (engine:{station_id}:in / :out) — 확인 1 확정 후 구현."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("확인 1(통신 채널) 확정 후 구현")


def make_transport(kind: str) -> Transport:
    if kind == "stdout":
        return StdoutTransport()
    if kind == "redis":
        return RedisTransport()
    raise ValueError(f"unknown transport kind: {kind}")
