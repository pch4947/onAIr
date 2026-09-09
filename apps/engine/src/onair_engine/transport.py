"""백엔드 어댑터 — 설계 문서 6장.

통신 채널이 미확정(확인 1)이므로 stdout 더미가 기본이고, M0 관통용으로 HTTP push
어댑터를 둔다. Redis Streams 어댑터는 1주차 계약 확정 후 구현한다.

어댑터를 교체해도 Transport 프로토콜만 지키면 엔진 본체는 수정되지 않는다.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import urllib.error
import urllib.request
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


class HttpTransport:
    """M0 관통용 HTTP push 어댑터 — 백엔드 REST 엔드포인트로 제출/통보한다.

    엔진은 클라이언트 전용이라 포트를 열지 않는다. 오디오 파일은 페이로드에 담지 않고
    공유 파일시스템에 쓴 뒤 audio_ref(루트 기준 상대 경로)만 보낸다 (설계 문서 6장).

    표준 라이브러리만 사용한다. urlopen은 블로킹이므로 to_thread로 감싸
    편성 관리자 루프를 막지 않는다.
    """

    def __init__(self, base_url: str, *, token: str | None = None,
                 timeout: float = 5.0, retries: int = 3):
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._retries = max(1, retries)

    async def publish_segment(self, sub: SegmentSubmission) -> None:
        await self._post("/api/engine/segments", dataclasses.asdict(sub))

    async def notify_request_state(self, request_id: str, state: RequestState) -> None:
        await self._post(
            f"/api/engine/requests/{request_id}/state",
            {"request_id": request_id, "state": str(state), "at": time.time()},
        )

    async def events(self) -> AsyncIterator[dict]:
        # TODO(M3): 백엔드 -> 엔진 이벤트 수신 (5.2절). HTTP로 확정되면 엔진이 수신 서버를
        # 열어야 하고, Redis로 확정되면 RedisTransport가 스트림을 폴링한다 (확인 1).
        while True:
            await asyncio.sleep(3600)
            yield {}

    async def _post(self, path: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        for attempt in range(self._retries):
            last = attempt == self._retries - 1
            try:
                await asyncio.to_thread(self._send, path, body)
                return
            except urllib.error.HTTPError as exc:
                # 4xx는 계약 위반·인증 실패 — 재시도해도 결과가 같으므로 즉시 포기한다
                if exc.code < 500 or last:
                    self._give_up(path, payload, exc)
                    return
            except OSError as exc:  # 연결 거부·타임아웃 — 백엔드가 아직 안 떴을 수 있다
                if last:
                    self._give_up(path, payload, exc)
                    return
            await asyncio.sleep(0.5 * (attempt + 1))

    def _send(self, path: str, body: bytes) -> None:
        req = urllib.request.Request(self._base + path, data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            resp.read()

    @staticmethod
    def _give_up(path: str, payload: dict, exc: Exception) -> None:
        # 제출 실패가 방송을 멈추게 하지는 않는다 — 로그만 남기고 다음 세그먼트로 넘어간다.
        # TODO(M2): 실패분 재제출 큐. 지금은 유실을 허용하고 로그로만 관찰한다.
        print(f"TRANSPORT_FAILED {path} id={payload.get('id')} {exc!r}", flush=True)


class RedisTransport:
    """Redis Streams 어댑터 (engine:{station_id}:in / :out) — 확인 1 확정 후 구현."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError("확인 1(통신 채널) 확정 후 구현")


def make_transport(kind: str, *, base_url: str = "http://localhost:3000",
                   token: str | None = None) -> Transport:
    if kind == "stdout":
        return StdoutTransport()
    if kind == "http":
        return HttpTransport(base_url, token=token)
    if kind == "redis":
        return RedisTransport()
    raise ValueError(f"unknown transport kind: {kind}")
