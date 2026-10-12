"""다시듣기(VOD) 세션 저장소와 API — 목록 조회, 소유권, 북마크."""
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from redis.exceptions import ConnectionError

from app.auth import get_current_user
from app.board import LikeStore
from app.config import Settings
from app.main import create_app
from app.replays import ReplaySessionStore
from app.stations import StationStore


class FakeReplayPipeline:
    """ReplaySessionStore(hash/zset)와 재사용된 LikeStore(set/zset) 명령을 모두 지원한다 —
    실제 앱에서 두 store가 하나의 공유 Redis 클라이언트를 쓰는 것과 동일하게 맞춘 것."""

    def __init__(self, redis):
        self.redis = redis
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def zrevrange(self, key, start, stop):
        self.calls.append(("zrevrange", key, start, stop))

    def zcard(self, key):
        self.calls.append(("zcard", key))

    def hset(self, key, field, value):
        self.calls.append(("hset", key, field, value))

    def hdel(self, key, field):
        self.calls.append(("hdel", key, field))

    def zrem(self, key, member):
        self.calls.append(("zrem", key, member))

    def zadd(self, key, mapping):
        self.calls.append(("zadd", key, mapping))

    def scard(self, key):
        self.calls.append(("scard", key))

    def sismember(self, key, member):
        self.calls.append(("sismember", key, member))

    async def execute(self):
        results = []
        for call in self.calls:
            if call[0] == "zrevrange":
                _, key, start, stop = call
                members = sorted(self.redis.zsets.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
                ids = [member for member, _ in members]
                stop = len(ids) - 1 if stop == -1 else stop
                results.append(ids[start:stop + 1])
            elif call[0] == "zcard":
                results.append(len(self.redis.zsets.get(call[1], {})))
            elif call[0] == "hset":
                _, key, field, value = call
                self.redis.hashes.setdefault(key, {})[field] = value
                results.append(1)
            elif call[0] == "hdel":
                _, key, field = call
                removed = self.redis.hashes.get(key, {}).pop(field, None)
                results.append(1 if removed is not None else 0)
            elif call[0] == "zrem":
                _, key, member = call
                removed = self.redis.zsets.get(key, {}).pop(member, None)
                results.append(1 if removed is not None else 0)
            elif call[0] == "zadd":
                _, key, mapping = call
                self.redis.zsets.setdefault(key, {}).update(mapping)
                results.append(len(mapping))
            elif call[0] == "scard":
                results.append(len(self.redis.sets.get(call[1], set())))
            elif call[0] == "sismember":
                results.append(call[2] in self.redis.sets.get(call[1], set()))
        return results


class FakeReplayRedis:
    """ReplaySessionStore(hash/zset), 재사용된 LikeStore(set/zset),
    station 소유권 조회에 쓰는 일반 GET까지 하나의 가짜 클라이언트로 전부 지원."""

    def __init__(self):
        self.hashes = {}
        self.zsets = {}
        self.strings = {}
        self.sets = {}

    def pipeline(self, transaction=True):
        return FakeReplayPipeline(self)

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def hmget(self, key, fields):
        h = self.hashes.get(key, {})
        return [h.get(f) for f in fields]

    async def get(self, key):
        return self.strings.get(key)

    async def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    async def srem(self, key, member):
        self.sets.setdefault(key, set()).discard(member)

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zrem(self, key, member):
        self.zsets.setdefault(key, {}).pop(member, None)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def delete(self, key):
        self.sets.pop(key, None)
        self.hashes.pop(key, None)
        self.zsets.pop(key, None)
        self.strings.pop(key, None)


class ReplaySessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_then_list_shows_open_session(self):
        store = ReplaySessionStore(FakeReplayRedis(), "st1")
        await store.start("sess1", 1000.0)
        sessions, total = await store.list(0, 10)
        self.assertEqual(total, 1)
        self.assertEqual(sessions[0]["id"], "sess1")
        self.assertIsNone(sessions[0]["endedAt"])

    async def test_end_sets_ended_at_without_losing_started_at(self):
        store = ReplaySessionStore(FakeReplayRedis(), "st1")
        await store.start("sess1", 1000.0)
        await store.end("sess1", 1060.0)
        sessions, _ = await store.list(0, 10)
        self.assertIsNotNone(sessions[0]["startedAt"])
        self.assertIsNotNone(sessions[0]["endedAt"])

    async def test_end_on_unknown_session_is_a_noop(self):
        store = ReplaySessionStore(FakeReplayRedis(), "st1")
        await store.end("missing", 1000.0)  # 예외가 발생하면 안 됨

    async def test_list_orders_newest_first_and_paginates(self):
        redis = FakeReplayRedis()
        store = ReplaySessionStore(redis, "st1")
        for i in range(5):
            await store.start(f"s{i}", 1000.0 + i)
        sessions, total = await store.list(0, 3)
        self.assertEqual(total, 5)
        self.assertEqual([s["id"] for s in sessions], ["s4", "s3", "s2"])
        sessions, total = await store.list(3, 3)
        self.assertEqual([s["id"] for s in sessions], ["s1", "s0"])

    async def test_get_many_skips_missing_ids(self):
        store = ReplaySessionStore(FakeReplayRedis(), "st1")
        await store.start("sess1", 1000.0)
        result = await store.get_many(["sess1", "missing"])
        self.assertEqual(set(result), {"sess1"})

    async def test_get_many_empty_input_returns_empty(self):
        store = ReplaySessionStore(FakeReplayRedis(), "st1")
        self.assertEqual(await store.get_many([]), {})


def make_app(redis=None, owner_id=None, hls_dir=None):
    app = create_app(Settings("127.0.0.1", 3000))
    redis = redis or FakeReplayRedis()
    app.state.redis = redis
    app.state.stations = StationStore(redis, app.state.settings.station_id)
    app.state.replay_sessions = ReplaySessionStore(redis, app.state.settings.station_id)
    app.state.replay_bookmarks = LikeStore(
        redis, f"backend:{app.state.settings.station_id}:replay-bookmarks",
        f"backend:{app.state.settings.station_id}:user-bookmarked-replays")
    app.state.hls = SimpleNamespace(directory=hls_dir or Path('.'))
    if owner_id:
        redis.strings[app.state.stations.key] = json.dumps({"owner_id": owner_id})
    return app, redis


class ReplayApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_replays_requires_login(self):
        app, _ = make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/stations/st_local_dev/replays")
            self.assertEqual(res.status_code, 401)

    async def test_list_replays_unknown_station_404s(self):
        app, _ = make_app()
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/stations/some-other-station/replays")
            self.assertEqual(res.status_code, 404)

    async def test_list_replays_includes_bookmark_status(self):
        app, redis = make_app()
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        await app.state.replay_sessions.start("sess1", 1000.0)
        await app.state.replay_sessions.end("sess1", 1060.0)
        await app.state.replay_bookmarks.like("sess1", "u1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/stations/st_local_dev/replays")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["total"], 1)
            replay = body["replays"][0]
            self.assertEqual(replay["id"], "sess1")
            self.assertTrue(replay["bookmarked"])
            self.assertEqual(replay["bookmarkCount"], 1)
            self.assertEqual(replay["durationSeconds"], 60)
            self.assertFalse(replay["ongoing"])
            self.assertEqual(replay["vodUrl"], "/hls/st_local_dev/vod/sess1/index.m3u8")

    async def test_ongoing_session_has_no_ended_at_and_is_ongoing(self):
        app, _ = make_app()
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        await app.state.replay_sessions.start("sess1", 1000.0)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/stations/st_local_dev/replays")
            replay = res.json()["replays"][0]
            self.assertTrue(replay["ongoing"])
            self.assertIsNone(replay["endedAt"])

    async def test_list_replays_handles_redis_failure(self):
        app, _ = make_app()
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        app.state.replay_sessions = SimpleNamespace(
            list=AsyncMock(side_effect=ConnectionError("offline")))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/stations/st_local_dev/replays")
            self.assertEqual(res.status_code, 503)

    async def test_mine_returns_sessions_for_owner(self):
        app, _ = make_app(owner_id="u1")
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        await app.state.replay_sessions.start("sess1", 1000.0)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/stations/st_local_dev/replays/mine")
            self.assertEqual(res.json()["total"], 1)

    async def test_mine_is_empty_for_non_owner(self):
        app, _ = make_app(owner_id="u1")
        app.dependency_overrides[get_current_user] = lambda: {"id": "u2", "name": "y"}
        await app.state.replay_sessions.start("sess1", 1000.0)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/stations/st_local_dev/replays/mine")
            self.assertEqual(res.json(), {"replays": [], "total": 0})

    async def test_mine_is_empty_when_no_station_record_exists(self):
        app, _ = make_app()  # owner_id 없음 -> station 레코드가 아예 기록되지 않음
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        await app.state.replay_sessions.start("sess1", 1000.0)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/stations/st_local_dev/replays/mine")
            self.assertEqual(res.json(), {"replays": [], "total": 0})

    async def test_bookmark_and_unbookmark_toggle(self):
        app, _ = make_app()
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        await app.state.replay_sessions.start("sess1", 1000.0)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/replays/sess1/bookmark")
            self.assertEqual(res.json(), {"bookmarkCount": 1, "bookmarked": True})
            res = await client.delete("/api/replays/sess1/bookmark")
            self.assertEqual(res.json(), {"bookmarkCount": 0, "bookmarked": False})

    async def test_bookmarked_list_shows_only_my_bookmarks_most_recent_first(self):
        app, _ = make_app()
        await app.state.replay_sessions.start("sess1", 1000.0)
        await app.state.replay_sessions.start("sess2", 1001.0)

        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            await client.post("/api/replays/sess1/bookmark")
            await client.post("/api/replays/sess2/bookmark")
            res = await client.get("/api/replays/bookmarked")
            body = res.json()
            self.assertEqual(body["total"], 2)
            self.assertEqual([r["id"] for r in body["replays"]], ["sess2", "sess1"])

        app.dependency_overrides[get_current_user] = lambda: {"id": "u2", "name": "y"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/replays/bookmarked")
            self.assertEqual(res.json(), {"replays": [], "total": 0})

    async def test_bookmark_requires_login(self):
        app, _ = make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/replays/sess1/bookmark")
            self.assertEqual(res.status_code, 401)

    async def test_delete_requires_login(self):
        app, _ = make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.delete("/api/replays/sess1")
            self.assertEqual(res.status_code, 401)

    async def test_delete_rejects_non_owner(self):
        app, _ = make_app(owner_id="u1")
        app.dependency_overrides[get_current_user] = lambda: {"id": "u2", "name": "y"}
        await app.state.replay_sessions.start("sess1", 1000.0)
        await app.state.replay_sessions.end("sess1", 1060.0)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.delete("/api/replays/sess1")
            self.assertEqual(res.status_code, 403)
        sessions, _ = await app.state.replay_sessions.list(0, 10)
        self.assertEqual([s["id"] for s in sessions], ["sess1"])  # 삭제되지 않고 그대로 남아있어야 함

    async def test_delete_rejects_unknown_session(self):
        app, _ = make_app(owner_id="u1")
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.delete("/api/replays/missing")
            self.assertEqual(res.status_code, 404)

    async def test_delete_rejects_ongoing_session(self):
        app, _ = make_app(owner_id="u1")
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        await app.state.replay_sessions.start("sess1", 1000.0)  # endedAt 없음 == 진행 중
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.delete("/api/replays/sess1")
            self.assertEqual(res.status_code, 409)
        sessions, _ = await app.state.replay_sessions.list(0, 10)
        self.assertEqual([s["id"] for s in sessions], ["sess1"])

    async def test_delete_removes_session_record_and_vod_files(self):
        with tempfile.TemporaryDirectory() as directory:
            hls_dir = Path(directory)
            app, _ = make_app(owner_id="u1", hls_dir=hls_dir)
            app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
            await app.state.replay_sessions.start("sess1", 1000.0)
            await app.state.replay_sessions.end("sess1", 1060.0)
            vod_dir = hls_dir / "vod" / "sess1"
            vod_dir.mkdir(parents=True)
            (vod_dir / "index.m3u8").write_text("#EXTM3U\n")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                res = await client.delete("/api/replays/sess1")
                self.assertEqual(res.status_code, 204)
            sessions, total = await app.state.replay_sessions.list(0, 10)
            self.assertEqual((sessions, total), ([], 0))
            self.assertFalse(vod_dir.exists())

    async def test_delete_with_no_vod_directory_on_disk_still_succeeds(self):
        # 세션 레코드는 있지만 실제 파일이 이미 지워졌거나 생긴 적 없는 경우에도 에러 없이 처리.
        with tempfile.TemporaryDirectory() as directory:
            app, _ = make_app(owner_id="u1", hls_dir=Path(directory))
            app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
            await app.state.replay_sessions.start("sess1", 1000.0)
            await app.state.replay_sessions.end("sess1", 1060.0)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                res = await client.delete("/api/replays/sess1")
                self.assertEqual(res.status_code, 204)

    async def test_delete_cleans_up_other_users_bookmarks(self):
        # 방송 주인이 녹음을 삭제하면, 그걸 북마크했던 다른 사람의 저장 목록/카운트에
        # 고아 항목으로 남으면 안 된다 (total이 안 맞게 되는 버그 재현 + 수정 확인).
        with tempfile.TemporaryDirectory() as directory:
            app, _ = make_app(owner_id="owner1", hls_dir=Path(directory))
            await app.state.replay_sessions.start("sess1", 1000.0)
            await app.state.replay_sessions.end("sess1", 1060.0)
            await app.state.replay_bookmarks.like("sess1", "bookmarker1")
            await app.state.replay_bookmarks.like("sess1", "bookmarker2")

            app.dependency_overrides[get_current_user] = lambda: {"id": "owner1", "name": "x"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                res = await client.delete("/api/replays/sess1")
                self.assertEqual(res.status_code, 204)

            for bookmarker_id in ("bookmarker1", "bookmarker2"):
                app.dependency_overrides[get_current_user] = lambda uid=bookmarker_id: {"id": uid, "name": "x"}
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                    res = await client.get("/api/replays/bookmarked")
                    body = res.json()
                    self.assertEqual(body, {"replays": [], "total": 0})  # total도 0이어야 함, 고아로 안 남아야
