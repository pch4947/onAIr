import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from redis.exceptions import ConnectionError

from app.config import Settings
from app.redis_connection import redis_health, redis_lifespan


class RedisConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping_success_and_disconnection(self):
        client = SimpleNamespace(ping=AsyncMock(return_value=True))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=client)))
        self.assertEqual(await redis_health(request), {"ok": True, "redis": "connected"})
        client.ping.side_effect = ConnectionError("private connection details")
        response = await redis_health(request)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"private", response.body)
        client.ping.side_effect = TimeoutError()
        self.assertEqual((await redis_health(request)).status_code, 503)

    async def test_lifespan_closes_client_even_on_failure(self):
        client = SimpleNamespace(aclose=AsyncMock())
        app = SimpleNamespace(state=SimpleNamespace(settings=Settings("127.0.0.1", 3000)))
        with patch("app.redis_connection.Redis.from_url", return_value=client):
            with self.assertRaises(RuntimeError):
                async with redis_lifespan(app):
                    self.assertIs(app.state.redis, client)
                    raise RuntimeError("shutdown")
        client.aclose.assert_awaited_once()
