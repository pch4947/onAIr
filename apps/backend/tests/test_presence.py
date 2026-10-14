"""청취 화면 시청자 수 — 하트비트 기반 presence 카운팅."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import Response
from redis.exceptions import ConnectionError

from app.api import BroadcastState, PresenceStore, stream_state
from app.config import Settings
from app.main import create_app


class FakePresenceRedis:
    def __init__(self):
        self.zsets = {}

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zremrangebyscore(self, key, min_score, max_score):
        members = self.zsets.get(key, {})
        for member, score in list(members.items()):
            if min_score <= score <= max_score:
                del members[member]

    async def zcard(self, key):
        return len(self.zsets.get(key, {}))


class PresenceStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_then_count(self):
        store = PresenceStore(FakePresenceRedis(), "st1")
        await store.heartbeat("client-a", 1000.0)
        await store.heartbeat("client-b", 1000.0)
        self.assertEqual(await store.count(1000.0), 2)

    async def test_repeat_heartbeat_from_same_client_does_not_double_count(self):
        store = PresenceStore(FakePresenceRedis(), "st1")
        await store.heartbeat("client-a", 1000.0)
        await store.heartbeat("client-a", 1010.0)
        self.assertEqual(await store.count(1010.0), 1)

    async def test_stale_heartbeat_expires_out_of_count(self):
        store = PresenceStore(FakePresenceRedis(), "st1")
        await store.heartbeat("client-a", 1000.0)
        # 윈도우(30초)보다 오래 지난 뒤 조회하면 제외돼야 함.
        self.assertEqual(await store.count(1000.0 + 31), 0)

    async def test_count_just_inside_window_still_counts(self):
        store = PresenceStore(FakePresenceRedis(), "st1")
        await store.heartbeat("client-a", 1000.0)
        self.assertEqual(await store.count(1000.0 + 29.9), 1)

    async def test_count_at_exact_window_boundary_excluded(self):
        # ZREMRANGEBYSCORE는 양 끝 포함이라, 정확히 window초 지난 시점엔 제거된다.
        store = PresenceStore(FakePresenceRedis(), "st1")
        await store.heartbeat("client-a", 1000.0)
        self.assertEqual(await store.count(1000.0 + 30), 0)

    async def test_mixed_fresh_and_stale_clients(self):
        store = PresenceStore(FakePresenceRedis(), "st1")
        await store.heartbeat("stale", 1000.0)
        await store.heartbeat("fresh", 1025.0)
        self.assertEqual(await store.count(1031.0), 1)


class PresenceApiTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self):
        app = create_app(Settings("127.0.0.1", 3000))
        redis = FakePresenceRedis()
        app.state.redis = redis
        app.state.presence = PresenceStore(redis, app.state.settings.station_id)
        return app

    async def test_heartbeat_accepts_valid_client_id(self):
        app = self.make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/stations/st_local_dev/presence", json={"clientId": "abc-123"})
            self.assertEqual(res.status_code, 204)

    async def test_heartbeat_rejects_unknown_station(self):
        app = self.make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/stations/some-other-station/presence", json={"clientId": "abc"})
            self.assertEqual(res.status_code, 404)

    async def test_heartbeat_rejects_empty_client_id(self):
        app = self.make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/stations/st_local_dev/presence", json={"clientId": ""})
            self.assertEqual(res.status_code, 422)

    async def test_heartbeat_requires_no_login(self):
        # 로그인 여부와 무관하게 익명으로 카운트되는 공개 엔드포인트여야 함.
        app = self.make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/stations/st_local_dev/presence", json={"clientId": "anon-1"})
            self.assertEqual(res.status_code, 204)

    async def test_two_heartbeats_then_count_reflects_both(self):
        app = self.make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            await client.post("/api/stations/st_local_dev/presence", json={"clientId": "u1"})
            await client.post("/api/stations/st_local_dev/presence", json={"clientId": "u2"})
        count = await app.state.presence.count(time.time())
        self.assertEqual(count, 2)


class StreamStateViewerCountTests(unittest.IsolatedAsyncioTestCase):
    async def test_viewer_count_included_in_stream_state(self):
        settings = Settings('127.0.0.1', 3000)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            broadcast=BroadcastState(), settings=settings, hls=None,
            redis=SimpleNamespace(get=AsyncMock(return_value=None)),
            requests=SimpleNamespace(metrics=AsyncMock(return_value={})),
            presence=SimpleNamespace(count=AsyncMock(return_value=7)))))
        result = await stream_state(request, Response())
        self.assertEqual(result['viewerCount'], 7)

    async def test_viewer_count_is_none_not_zero_on_redis_failure(self):
        settings = Settings('127.0.0.1', 3000)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            broadcast=BroadcastState(), settings=settings, hls=None,
            redis=SimpleNamespace(get=AsyncMock(side_effect=ConnectionError('offline'))),
            requests=SimpleNamespace(metrics=AsyncMock(return_value={})),
            presence=SimpleNamespace(count=AsyncMock(side_effect=ConnectionError('offline'))))))
        result = await stream_state(request, Response())
        self.assertIsNone(result['viewerCount'])
