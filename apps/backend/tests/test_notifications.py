"""Per-user notification storage and API — creation, pagination, unread tracking, auth."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from redis.exceptions import ConnectionError

from app.auth import get_current_user
from app.config import Settings
from app.main import create_app
from app.notifications import NotificationStore


class FakeNotificationPipeline:
    def __init__(self, redis):
        self.redis = redis
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def hset(self, key, field, value):
        self.calls.append(("hset", key, field, value))

    def zadd(self, key, mapping):
        self.calls.append(("zadd", key, mapping))

    def sadd(self, key, member):
        self.calls.append(("sadd", key, member))

    def zrevrange(self, key, start, stop):
        self.calls.append(("zrevrange", key, start, stop))

    def zcard(self, key):
        self.calls.append(("zcard", key))

    def hmget(self, key, fields):
        self.calls.append(("hmget", key, fields))

    def smembers(self, key):
        self.calls.append(("smembers", key))

    def hdel(self, key, field):
        self.calls.append(("hdel", key, field))

    def zrem(self, key, member):
        self.calls.append(("zrem", key, member))

    def srem(self, key, member):
        self.calls.append(("srem", key, member))

    async def execute(self):
        results = []
        for call in self.calls:
            if call[0] == "hset":
                _, key, field, value = call
                self.redis.hashes.setdefault(key, {})[field] = value
                results.append(1)
            elif call[0] == "zadd":
                _, key, mapping = call
                self.redis.zsets.setdefault(key, {}).update(mapping)
                results.append(len(mapping))
            elif call[0] == "sadd":
                _, key, member = call
                self.redis.sets.setdefault(key, set()).add(member)
                results.append(1)
            elif call[0] == "hdel":
                _, key, field = call
                removed = self.redis.hashes.get(key, {}).pop(field, None)
                results.append(1 if removed is not None else 0)
            elif call[0] == "zrem":
                _, key, member = call
                removed = self.redis.zsets.get(key, {}).pop(member, None)
                results.append(1 if removed is not None else 0)
            elif call[0] == "srem":
                _, key, member = call
                self.redis.sets.setdefault(key, set()).discard(member)
                results.append(1)
            elif call[0] == "zrevrange":
                _, key, start, stop = call
                members = sorted(self.redis.zsets.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
                ids = [member for member, _ in members]
                stop = len(ids) - 1 if stop == -1 else stop
                results.append(ids[start:stop + 1])
            elif call[0] == "zcard":
                results.append(len(self.redis.zsets.get(call[1], {})))
            elif call[0] == "hmget":
                _, key, fields = call
                h = self.redis.hashes.get(key, {})
                results.append([h.get(f) for f in fields])
            elif call[0] == "smembers":
                results.append(set(self.redis.sets.get(call[1], set())))
        return results


class FakeNotificationRedis:
    def __init__(self):
        self.hashes = {}
        self.zsets = {}
        self.sets = {}

    def pipeline(self, transaction=True):
        return FakeNotificationPipeline(self)

    async def scard(self, key):
        return len(self.sets.get(key, set()))

    async def srem(self, key, member):
        self.sets.setdefault(key, set()).discard(member)

    async def delete(self, key):
        self.sets.pop(key, None)
        self.hashes.pop(key, None)
        self.zsets.pop(key, None)


class NotificationStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_is_unread_and_appears_in_list(self):
        store = NotificationStore(FakeNotificationRedis())
        await store.create("u1", "comment", "새 댓글이 달렸어요", "x 님이 댓글을 남겼어요")
        notifications, total = await store.list("u1", 0, 10)
        self.assertEqual(total, 1)
        self.assertEqual(notifications[0]["title"], "새 댓글이 달렸어요")
        self.assertTrue(notifications[0]["isUnread"])

    async def test_create_stores_and_returns_data_payload(self):
        store = NotificationStore(FakeNotificationRedis())
        await store.create("u1", "comment", "t", "d", data={"postId": "post1"})
        notifications, _ = await store.list("u1", 0, 10)
        self.assertEqual(notifications[0]["data"], {"postId": "post1"})

    async def test_create_without_data_defaults_to_empty_dict(self):
        store = NotificationStore(FakeNotificationRedis())
        await store.create("u1", "comment", "t", "d")
        notifications, _ = await store.list("u1", 0, 10)
        self.assertEqual(notifications[0]["data"], {})

    async def test_list_sorts_newest_first(self):
        store = NotificationStore(FakeNotificationRedis())
        await store.create("u1", "comment", "first", "d1")
        await store.create("u1", "comment", "second", "d2")
        notifications, _ = await store.list("u1", 0, 10)
        self.assertEqual([n["title"] for n in notifications], ["second", "first"])

    async def test_list_is_scoped_per_user(self):
        store = NotificationStore(FakeNotificationRedis())
        await store.create("u1", "comment", "for u1", "d")
        await store.create("u2", "comment", "for u2", "d")
        notifications, total = await store.list("u1", 0, 10)
        self.assertEqual(total, 1)
        self.assertEqual(notifications[0]["title"], "for u1")

    async def test_list_pagination(self):
        store = NotificationStore(FakeNotificationRedis())
        for i in range(5):
            await store.create("u1", "comment", f"n{i}", "d")
        notifications, total = await store.list("u1", 1, 2)
        self.assertEqual(total, 5)
        self.assertEqual([n["title"] for n in notifications], ["n3", "n2"])

    async def test_list_on_empty_user_returns_empty(self):
        store = NotificationStore(FakeNotificationRedis())
        notifications, total = await store.list("nobody", 0, 10)
        self.assertEqual((notifications, total), ([], 0))

    async def test_mark_read_clears_unread_flag_without_removing_entry(self):
        store = NotificationStore(FakeNotificationRedis())
        entry = await store.create("u1", "comment", "t", "d")
        await store.mark_read("u1", entry["id"])
        notifications, total = await store.list("u1", 0, 10)
        self.assertEqual(total, 1)
        self.assertFalse(notifications[0]["isUnread"])

    async def test_mark_read_on_unknown_id_is_a_noop(self):
        store = NotificationStore(FakeNotificationRedis())
        await store.mark_read("u1", "missing")  # must not raise

    async def test_mark_all_read_clears_unread_count(self):
        store = NotificationStore(FakeNotificationRedis())
        await store.create("u1", "comment", "a", "d")
        await store.create("u1", "comment", "b", "d")
        self.assertEqual(await store.unread_count("u1"), 2)
        await store.mark_all_read("u1")
        self.assertEqual(await store.unread_count("u1"), 0)
        notifications, total = await store.list("u1", 0, 10)
        self.assertEqual(total, 2)
        self.assertTrue(all(not n["isUnread"] for n in notifications))

    async def test_delete_removes_one_notification(self):
        store = NotificationStore(FakeNotificationRedis())
        keep = await store.create("u1", "comment", "keep", "d")
        drop = await store.create("u1", "comment", "drop", "d")
        await store.delete("u1", drop["id"])
        notifications, total = await store.list("u1", 0, 10)
        self.assertEqual(total, 1)
        self.assertEqual(notifications[0]["id"], keep["id"])

    async def test_delete_also_clears_unread_count(self):
        store = NotificationStore(FakeNotificationRedis())
        entry = await store.create("u1", "comment", "t", "d")
        await store.delete("u1", entry["id"])
        self.assertEqual(await store.unread_count("u1"), 0)

    async def test_delete_on_unknown_id_is_a_noop(self):
        store = NotificationStore(FakeNotificationRedis())
        await store.delete("u1", "missing")  # must not raise

    async def test_delete_is_scoped_per_user(self):
        store = NotificationStore(FakeNotificationRedis())
        entry = await store.create("u1", "comment", "t", "d")
        await store.delete("u2", entry["id"])
        notifications, total = await store.list("u1", 0, 10)
        self.assertEqual(total, 1)

    async def test_delete_all_clears_everything(self):
        store = NotificationStore(FakeNotificationRedis())
        await store.create("u1", "comment", "a", "d")
        await store.create("u1", "comment", "b", "d")
        await store.delete_all("u1")
        notifications, total = await store.list("u1", 0, 10)
        self.assertEqual((notifications, total), ([], 0))
        self.assertEqual(await store.unread_count("u1"), 0)


class NotificationApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_requires_login(self):
        app = create_app(Settings("127.0.0.1", 3000))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/notifications")
            self.assertEqual(res.status_code, 401)

    async def test_list_returns_notifications_and_unread_count(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        app.state.notifications = NotificationStore(FakeNotificationRedis())
        await app.state.notifications.create("u1", "comment", "새 댓글이 달렸어요", "d")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/notifications")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["total"], 1)
            self.assertEqual(body["unreadCount"], 1)
            self.assertEqual(body["notifications"][0]["title"], "새 댓글이 달렸어요")

    async def test_mark_read_then_list_reflects_it(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        app.state.notifications = NotificationStore(FakeNotificationRedis())
        entry = await app.state.notifications.create("u1", "comment", "t", "d")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post(f"/api/notifications/{entry['id']}/read")
            self.assertEqual(res.status_code, 200)
            res = await client.get("/api/notifications")
            self.assertEqual(res.json()["unreadCount"], 0)

    async def test_mark_all_read(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        app.state.notifications = NotificationStore(FakeNotificationRedis())
        await app.state.notifications.create("u1", "comment", "a", "d")
        await app.state.notifications.create("u1", "comment", "b", "d")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/notifications/read-all")
            self.assertEqual(res.status_code, 200)
            res = await client.get("/api/notifications")
            self.assertEqual(res.json()["unreadCount"], 0)

    async def test_redis_failure_returns_503(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        failing = FakeNotificationRedis()
        failing.pipeline = lambda transaction=True: (_ for _ in ()).throw(ConnectionError("offline"))
        app.state.notifications = NotificationStore(failing)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/notifications")
            self.assertEqual(res.status_code, 503)

    async def test_delete_one_then_list_reflects_it(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        app.state.notifications = NotificationStore(FakeNotificationRedis())
        keep = await app.state.notifications.create("u1", "comment", "keep", "d")
        drop = await app.state.notifications.create("u1", "comment", "drop", "d")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.delete(f"/api/notifications/{drop['id']}")
            self.assertEqual(res.status_code, 200)
            res = await client.get("/api/notifications")
            body = res.json()
            self.assertEqual(body["total"], 1)
            self.assertEqual(body["notifications"][0]["id"], keep["id"])

    async def test_delete_requires_login(self):
        app = create_app(Settings("127.0.0.1", 3000))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.delete("/api/notifications/n1")
            self.assertEqual(res.status_code, 401)

    async def test_delete_all_then_list_is_empty(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        app.state.notifications = NotificationStore(FakeNotificationRedis())
        await app.state.notifications.create("u1", "comment", "a", "d")
        await app.state.notifications.create("u1", "comment", "b", "d")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.delete("/api/notifications")
            self.assertEqual(res.status_code, 200)
            res = await client.get("/api/notifications")
            body = res.json()
            self.assertEqual(body["total"], 0)
            self.assertEqual(body["unreadCount"], 0)

    async def test_delete_all_requires_login(self):
        app = create_app(Settings("127.0.0.1", 3000))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.delete("/api/notifications")
            self.assertEqual(res.status_code, 401)


if __name__ == "__main__":
    unittest.main()
