"""백엔드 어댑터 — 설계 문서 6장.

stdout 더미가 기본이고, M0 관통용 HTTP push 어댑터와 Redis Streams 어댑터를 둔다.
Redis 계약(스트림·envelope·전달 보장)은 docs/ENGINE_REDIS_CONTRACT.md에 있다.

어댑터를 교체해도 Transport 프로토콜만 지키면 엔진 본체는 수정되지 않는다.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from typing import Protocol

from .domain import RequestState, SegmentSubmission, new_id


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
        # HTTP로는 백엔드 -> 엔진 이벤트를 받지 않는다 (엔진이 포트를 열지 않음). Redis를 쓴다.
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
    """Redis Streams 어댑터 — 계약은 docs/ENGINE_REDIS_CONTRACT.md.

    엔진 -> 백엔드: engine:{station_id}:out 에 XADD. 파이프라인이 파일을 다 쓴 뒤에만 호출된다.
    백엔드 -> 엔진: engine:{station_id}:in 을 소비 그룹 "engine"으로 읽고, 처리가 끝나면 XACK.
    """

    CONTRACT_VERSION = "1"
    GROUP = "engine"

    def __init__(self, url: str, station_id: str, *, client=None, consumer: str | None = None,
                 maxlen: int = 10_000, block_ms: int = 5_000):
        try:
            import redis.asyncio as redis_asyncio
            from redis.exceptions import RedisError, ResponseError
        except ImportError as exc:  # 선택 의존성 — 기동 시점에 실패시킨다
            raise RuntimeError('redis transport를 쓰려면 pip install -e ".[redis]" 가 필요합니다') from exc
        self._redis = client if client is not None else redis_asyncio.from_url(
            url, decode_responses=True)
        self._errors = (RedisError, OSError)
        self._response_error = ResponseError
        self.station_id = station_id
        self.out_stream = f"engine:{station_id}:out"
        self.in_stream = f"engine:{station_id}:in"
        # 재기동해도 같은 이름이어야 자기 pending을 되찾는다 — 호스트명 기준 (다중 엔진이면 명시)
        self._consumer = consumer or f"engine-{socket.gethostname()}"
        self._maxlen = maxlen
        self._block_ms = block_ms

    async def publish_segment(self, sub: SegmentSubmission) -> None:
        await self._publish("segment.submitted", dataclasses.asdict(sub))

    async def notify_request_state(self, request_id: str, state: RequestState) -> None:
        await self._publish("request.state", {"request_id": request_id, "state": str(state)})

    async def events(self) -> AsyncIterator[dict]:
        await self._ensure_group()
        # 이전 실행에서 받고 ACK 못 한 내 몫(pending, ID "0")부터 처리한 뒤 새 메시지(">")로 넘어간다
        cursor = "0"
        while True:
            resp = await self._redis.xreadgroup(
                self.GROUP, self._consumer, {self.in_stream: cursor}, count=10,
                block=None if cursor == "0" else self._block_ms,
            )
            entries = resp[0][1] if resp else []
            if cursor == "0" and not entries:
                cursor = ">"
                continue
            for entry_id, fields in entries:
                event = self._decode(entry_id, fields)
                if event is not None:
                    yield event  # 소비자가 처리를 마치고 다음 이벤트를 요청해야 ACK된다
                await self._redis.xack(self.in_stream, self.GROUP, entry_id)

    async def close(self) -> None:
        await self._redis.aclose()

    async def _publish(self, event_type: str, payload: dict) -> None:
        fields = {
            "type": event_type,
            "contract_version": self.CONTRACT_VERSION,
            "event_id": new_id("evt"),
            "station_id": self.station_id,
            "at": str(time.time()),
            "payload": json.dumps(payload, ensure_ascii=False, default=str),
        }
        try:
            await self._redis.xadd(self.out_stream, fields, maxlen=self._maxlen, approximate=True)
        except self._errors as exc:
            # HttpTransport와 같은 규칙: 제출 실패가 방송을 멈추지 않는다 (TODO(M2): 재제출 큐)
            print(f"TRANSPORT_FAILED {self.out_stream} {event_type} {exc!r}", flush=True)

    async def _ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(self.in_stream, self.GROUP, id="0", mkstream=True)
        except self._response_error as exc:
            if "BUSYGROUP" not in str(exc):  # 이미 있으면 정상
                raise

    def _decode(self, entry_id: str, fields: dict | None) -> dict | None:
        try:
            return {
                "stream_id": entry_id,
                "type": fields["type"],
                "contract_version": fields.get("contract_version"),
                "event_id": fields.get("event_id"),
                "at": float(fields["at"]),
                "payload": json.loads(fields.get("payload") or "{}"),
            }
        except (KeyError, TypeError, ValueError) as exc:
            # 계약 위반 메시지를 붙잡고 있으면 뒤 메시지가 전부 막힌다 — 기록 후 ACK로 흘려보낸다
            print(f"TRANSPORT_BAD_EVENT {self.in_stream} {entry_id} {exc!r}", flush=True)
            return None


def make_transport(kind: str, *, base_url: str = "http://localhost:3000",
                   token: str | None = None, redis_url: str = "redis://localhost:6379/0",
                   station_id: str | None = None) -> Transport:
    if kind == "stdout":
        return StdoutTransport()
    if kind == "http":
        return HttpTransport(base_url, token=token)
    if kind == "redis":
        if station_id is None:
            raise ValueError("redis transport는 station_id가 필요합니다")
        return RedisTransport(redis_url, station_id)
    raise ValueError(f"unknown transport kind: {kind}")
