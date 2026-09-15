"""백엔드 어댑터 — HTTP 계약 v1의 세그먼트 제출을 담당한다."""
from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.request import Request, urlopen

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


class HttpTransport:
    """공용 오디오 폴더의 상대 경로와 메타데이터를 백엔드에 제출한다."""

    def __init__(self, *, base_url: str, audio_root: Path):
        self._endpoint = f"{base_url.rstrip('/')}/api/segments"
        self._audio_root = Path(audio_root).resolve()

    async def publish_segment(self, sub: SegmentSubmission) -> None:
        payload = dataclasses.asdict(sub)
        payload["contract_version"] = 1
        payload["audio_ref"] = Path(sub.audio_ref).resolve().relative_to(self._audio_root).as_posix()
        payload["kind"] = str(sub.kind)
        payload["created_at"] = datetime.fromtimestamp(sub.created_at, timezone.utc).isoformat().replace("+00:00", "Z")
        await asyncio.to_thread(self._post_json, payload)

    async def notify_request_state(self, request_id: str, state: RequestState) -> None:
        # 요청 상태 동기화는 M3 범위다.
        return None

    async def events(self) -> AsyncIterator[dict]:
        while True:
            await asyncio.sleep(3600)
            yield {}

    def _post_json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(self._endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=5) as response:
            if response.status != 201:
                raise RuntimeError(f"segment submission failed with HTTP {response.status}")


class RedisTransport:
    """Redis Streams 어댑터 (engine:{station_id}:in / :out) — 확인 1 확정 후 구현."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("확인 1(통신 채널) 확정 후 구현")


def make_transport(kind: str, *, base_url: str = "http://localhost:3000", audio_root: Path = Path("var/audio")) -> Transport:
    if kind == "stdout":
        return StdoutTransport()
    if kind == "http":
        return HttpTransport(base_url=base_url, audio_root=audio_root)
    if kind == "redis":
        return RedisTransport()
    raise ValueError(f"unknown transport kind: {kind}")
