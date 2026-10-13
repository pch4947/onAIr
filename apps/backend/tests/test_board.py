"""Board post/comment storage and API — auth, validation, pagination, and Redis failure handling."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from redis.exceptions import ConnectionError

from app.auth import get_current_user
from app.board import BoardStore, CommentStore, LikeStore
from app.config import Settings
from app.main import create_app


class BoardStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_stores_fields_and_generates_id(self):
        redis = SimpleNamespace(hset=AsyncMock())
        store = BoardStore(redis)
        entry = await store.create("u1", "라디오러버", "오늘 하루 위로가 필요해요", "사연입니다", None)
        self.assertEqual(entry["authorId"], "u1")
        self.assertEqual(entry["authorName"], "라디오러버")
        self.assertEqual(entry["title"], "오늘 하루 위로가 필요해요")
        self.assertIsNone(entry["songRequestUrl"])
        self.assertIn("id", entry)
        redis.hset.assert_awaited_once()
        self.assertEqual(redis.hset.await_args.args[0], store.key)
        self.assertEqual(redis.hset.await_args.args[1], entry["id"])

    async def test_list_sorts_newest_first(self):
        older = dict(id="a", authorId="u1", authorName="x", title="t1", body="b1",
                    songRequestUrl=None, createdAt="2026-01-01T00:00:00.000+00:00")
        newer = dict(id="b", authorId="u2", authorName="y", title="t2", body="b2",
                    songRequestUrl=None, createdAt="2026-01-02T00:00:00.000+00:00")
        redis = SimpleNamespace(hgetall=AsyncMock(return_value={
            "a": json.dumps(older, ensure_ascii=False), "b": json.dumps(newer, ensure_ascii=False)}))
        store = BoardStore(redis)
        posts = await store.list()
        self.assertEqual([post["id"] for post in posts], ["b", "a"])


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def lrange(self, key, start, stop):
        self.calls.append(("lrange", key, start, stop))

    def llen(self, key):
        self.calls.append(("llen", key))

    async def execute(self):
        results = []
        for call in self.calls:
            if call[0] == "lrange":
                _, key, start, stop = call
                items = self.redis.lists.get(key, [])
                stop = len(items) - 1 if stop == -1 else stop
                results.append(items[start:stop + 1])
            else:
                results.append(len(self.redis.lists.get(call[1], [])))
        return results


class FakeRedisList:
    def __init__(self, lists):
        self.lists = lists

    def pipeline(self, transaction=True):
        return FakePipeline(self)


class CommentStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_appends_and_list_paginates(self):
        redis = FakeRedisList({})
        store = CommentStore(redis)
        redis.rpush = AsyncMock(side_effect=lambda key, value: redis.lists.setdefault(key, []).append(value))
        for i in range(3):
            await store.create("post1", f"u{i}", f"user{i}", f"댓글 {i}")

        comments, total = await store.list("post1", offset=0, limit=2)
        self.assertEqual(total, 3)
        self.assertEqual([c["body"] for c in comments], ["댓글 0", "댓글 1"])

        comments, total = await store.list("post1", offset=2, limit=2)
        self.assertEqual(total, 3)
        self.assertEqual([c["body"] for c in comments], ["댓글 2"])

    async def test_list_empty_post_returns_nothing(self):
        redis = FakeRedisList({})
        store = CommentStore(redis)
        comments, total = await store.list("missing", offset=0, limit=10)
        self.assertEqual((comments, total), ([], 0))


class FakeSetPipeline:
    def __init__(self, redis):
        self.redis = redis
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def scard(self, key):
        self.calls.append(("scard", key))

    def sismember(self, key, member):
        self.calls.append(("sismember", key, member))

    def zrevrange(self, key, start, stop):
        self.calls.append(("zrevrange", key, start, stop))

    def zcard(self, key):
        self.calls.append(("zcard", key))

    async def execute(self):
        results = []
        for call in self.calls:
            if call[0] == "scard":
                results.append(len(self.redis.sets.get(call[1], set())))
            elif call[0] == "sismember":
                results.append(call[2] in self.redis.sets.get(call[1], set()))
            elif call[0] == "zrevrange":
                _, key, start, stop = call
                members = sorted(self.redis.zsets.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
                ids = [member for member, _ in members]
                stop = len(ids) - 1 if stop == -1 else stop
                results.append(ids[start:stop + 1])
            elif call[0] == "zcard":
                results.append(len(self.redis.zsets.get(call[1], {})))
        return results


class FakeRedisSet:
    def __init__(self, sets=None):
        self.sets = sets if sets is not None else {}
        self.zsets = {}

    async def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    async def srem(self, key, member):
        self.sets.setdefault(key, set()).discard(member)

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zrem(self, key, member):
        self.zsets.setdefault(key, {}).pop(member, None)

    def pipeline(self, transaction=True):
        return FakeSetPipeline(self)


class LikeStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_like_unlike_and_status(self):
        store = LikeStore(FakeRedisSet(), "prefix")
        self.assertEqual(await store.status("t1", "u1"), (0, False))

        await store.like("t1", "u1")
        self.assertEqual(await store.status("t1", "u1"), (1, True))

        await store.like("t1", "u2")
        self.assertEqual(await store.status("t1", "u1"), (2, True))

        await store.unlike("t1", "u1")
        self.assertEqual(await store.status("t1", "u1"), (1, False))

    async def test_status_many_mixes_liked_and_unliked_and_missing(self):
        store = LikeStore(FakeRedisSet(), "prefix")
        await store.like("a", "u1")
        await store.like("a", "u2")
        await store.like("b", "u2")
        result = await store.status_many(["a", "b", "c"], "u1")
        self.assertEqual(result, {"a": (2, True), "b": (1, False), "c": (0, False)})

    async def test_status_many_empty_returns_empty(self):
        store = LikeStore(FakeRedisSet(), "prefix")
        self.assertEqual(await store.status_many([], "u1"), {})


class LikeStoreUserIndexTests(unittest.IsolatedAsyncioTestCase):
    async def test_like_and_unlike_update_user_index_ordered_by_recency(self):
        store = LikeStore(FakeRedisSet(), "backend:board:post-likes", "backend:board:user-liked-posts")
        with patch("app.board.time", SimpleNamespace(time=Mock(side_effect=[1000.0, 1001.0]))):
            await store.like("p1", "u1")
            await store.like("p2", "u1")
        ids, total = await store.liked_by("u1", 0, 10)
        self.assertEqual(total, 2)
        self.assertEqual(ids, ["p2", "p1"])

        await store.unlike("p2", "u1")
        self.assertEqual(await store.liked_by("u1", 0, 10), (["p1"], 1))

    async def test_equal_timestamps_use_reverse_member_order(self):
        store = LikeStore(FakeRedisSet(), "likes", "user-index")
        with patch("app.board.time", SimpleNamespace(time=Mock(return_value=1000.0))):
            for post_id in ("p1", "p3", "p2"):
                await store.like(post_id, "u1")
        # Redis ZREVRANGE breaks score ties by reverse lexicographical member order.
        self.assertEqual(await store.liked_by("u1", 0, 10), (["p3", "p2", "p1"], 3))
        self.assertEqual(await store.liked_by("u1", 1, 1), (["p2"], 3))

    async def test_liked_by_paginates(self):
        store = LikeStore(FakeRedisSet(), "prefix", "user-index")
        for i in range(5):
            await store.like(f"p{i}", "u1")
        ids, total = await store.liked_by("u1", 0, 3)
        self.assertEqual(total, 5)
        self.assertEqual(len(ids), 3)

    async def test_comment_likes_skip_user_index_entirely(self):
        redis = FakeRedisSet()
        store = LikeStore(redis, "backend:board:comment-likes")  # no user_index_prefix
        await store.like("c1", "u1")
        self.assertEqual(redis.zsets, {})


class BoardStoreGetManyTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_many_returns_only_found_posts_by_id(self):
        post = dict(id="a", authorId="u1", authorName="x", title="t1", body="b1",
                   songRequestUrl=None, createdAt="2026-01-01T00:00:00.000+00:00")
        redis = SimpleNamespace(hmget=AsyncMock(return_value=[json.dumps(post, ensure_ascii=False), None]))
        store = BoardStore(redis)
        result = await store.get_many(["a", "missing"])
        self.assertEqual(set(result.keys()), {"a"})
        redis.hmget.assert_awaited_once_with(store.key, ["a", "missing"])

    async def test_get_many_empty_list_skips_redis_call(self):
        redis = SimpleNamespace(hmget=AsyncMock())
        store = BoardStore(redis)
        self.assertEqual(await store.get_many([]), {})
        redis.hmget.assert_not_called()


class MyAndLikedPostsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_mine_filters_by_author_and_paginates(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        raw = {}
        for i in range(3):
            post = dict(id=f"mine{i}", authorId="u1", authorName="x", title=f"t{i}", body="b",
                       songRequestUrl=None, createdAt=f"2026-01-0{i + 1}T00:00:00.000+00:00")
            raw[post["id"]] = json.dumps(post, ensure_ascii=False)
        other = dict(id="other1", authorId="u2", authorName="y", title="other", body="b",
                    songRequestUrl=None, createdAt="2026-01-04T00:00:00.000+00:00")
        raw["other1"] = json.dumps(other, ensure_ascii=False)
        app.state.board = BoardStore(SimpleNamespace(hgetall=AsyncMock(return_value=raw)))
        app.state.post_likes = LikeStore(FakeRedisSet(), "backend:board:post-likes",
                                         "backend:board:user-liked-posts")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts/mine")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["total"], 3)
            self.assertEqual({p["id"] for p in body["posts"]}, {"mine0", "mine1", "mine2"})
            self.assertTrue(all(p["authorId"] == "u1" for p in body["posts"]))

    async def test_mine_requires_login(self):
        app = create_app(Settings("127.0.0.1", 3000))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts/mine")
            self.assertEqual(res.status_code, 401)

    async def test_liked_returns_posts_in_recency_order(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        raw = {}
        for pid in ("p1", "p2"):
            post = dict(id=pid, authorId="u2", authorName="y", title=pid, body="b",
                       songRequestUrl=None, createdAt="2026-01-01T00:00:00.000+00:00")
            raw[pid] = json.dumps(post, ensure_ascii=False)
        app.state.board = BoardStore(SimpleNamespace(
            hmget=AsyncMock(side_effect=lambda key, ids: [raw.get(i) for i in ids])))
        app.state.post_likes = LikeStore(FakeRedisSet(), "backend:board:post-likes",
                                         "backend:board:user-liked-posts")
        with patch("app.board.time", SimpleNamespace(time=Mock(side_effect=[1000.0, 1001.0]))):
            await app.state.post_likes.like("p1", "u1")
            await app.state.post_likes.like("p2", "u1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts/liked")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["total"], 2)
            self.assertEqual([p["id"] for p in body["posts"]], ["p2", "p1"])
            self.assertTrue(all(p["liked"] for p in body["posts"]))

    async def test_liked_requires_login(self):
        app = create_app(Settings("127.0.0.1", 3000))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts/liked")
            self.assertEqual(res.status_code, 401)

    async def test_liked_handles_redis_failure(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        failing = SimpleNamespace(
            pipeline=lambda transaction=True: (_ for _ in ()).throw(ConnectionError("offline")))
        app.state.post_likes = LikeStore(failing, "backend:board:post-likes", "backend:board:user-liked-posts")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts/liked")
            self.assertEqual(res.status_code, 503)

    async def test_mine_pagination_boundaries(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        raw = {}
        for i in range(5):
            post = dict(id=f"m{i}", authorId="u1", authorName="x", title=f"t{i}", body="b",
                       songRequestUrl=None, createdAt=f"2026-01-0{i + 1}T00:00:00.000+00:00")
            raw[post["id"]] = json.dumps(post, ensure_ascii=False)
        app.state.board = BoardStore(SimpleNamespace(hgetall=AsyncMock(return_value=raw)))
        app.state.post_likes = LikeStore(FakeRedisSet(), "backend:board:post-likes",
                                         "backend:board:user-liked-posts")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            # last page is a partial page (5 items, limit 2 -> pages of 2,2,1)
            res = await client.get("/api/board/posts/mine", params={"offset": 4, "limit": 2})
            body = res.json()
            self.assertEqual(body["total"], 5)
            self.assertEqual(len(body["posts"]), 1)

            # offset at exactly total -> empty page, not an error
            res = await client.get("/api/board/posts/mine", params={"offset": 5, "limit": 2})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["posts"], [])
            self.assertEqual(res.json()["total"], 5)

            # offset past total -> still empty, not an error
            res = await client.get("/api/board/posts/mine", params={"offset": 100, "limit": 2})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["posts"], [])

    async def test_mine_empty_when_no_posts(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        app.state.board = BoardStore(SimpleNamespace(hgetall=AsyncMock(return_value={})))
        app.state.post_likes = LikeStore(FakeRedisSet(), "backend:board:post-likes",
                                         "backend:board:user-liked-posts")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts/mine")
            self.assertEqual(res.json(), {"posts": [], "total": 0})

    async def test_liked_pagination_boundaries(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        raw = {}
        for i in range(5):
            pid = f"l{i}"
            post = dict(id=pid, authorId="u2", authorName="y", title=pid, body="b",
                       songRequestUrl=None, createdAt="2026-01-01T00:00:00.000+00:00")
            raw[pid] = json.dumps(post, ensure_ascii=False)
        app.state.board = BoardStore(SimpleNamespace(
            hmget=AsyncMock(side_effect=lambda key, ids: [raw.get(i) for i in ids])))
        app.state.post_likes = LikeStore(FakeRedisSet(), "backend:board:post-likes",
                                         "backend:board:user-liked-posts")
        with patch("app.board.time", SimpleNamespace(time=Mock(side_effect=range(1000, 1005)))):
            for i in range(5):
                await app.state.post_likes.like(f"l{i}", "u1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts/liked", params={"offset": 4, "limit": 2})
            body = res.json()
            self.assertEqual(body["total"], 5)
            self.assertEqual(len(body["posts"]), 1)
            self.assertEqual(body["posts"][0]["id"], "l0")  # least-recently-liked, last page

            res = await client.get("/api/board/posts/liked", params={"offset": 10, "limit": 2})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["posts"], [])

    async def test_liking_own_post_shows_up_in_both_mine_and_liked(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        post = dict(id="own1", authorId="u1", authorName="x", title="내 글", body="b",
                   songRequestUrl=None, createdAt="2026-01-01T00:00:00.000+00:00")
        raw = {"own1": json.dumps(post, ensure_ascii=False)}
        app.state.board = BoardStore(SimpleNamespace(
            hgetall=AsyncMock(return_value=raw),
            hmget=AsyncMock(side_effect=lambda key, ids: [raw.get(i) for i in ids])))
        app.state.post_likes = LikeStore(FakeRedisSet(), "backend:board:post-likes",
                                         "backend:board:user-liked-posts")
        await app.state.post_likes.like("own1", "u1")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            mine = await client.get("/api/board/posts/mine")
            self.assertEqual([p["id"] for p in mine.json()["posts"]], ["own1"])
            self.assertTrue(mine.json()["posts"][0]["liked"])

            liked = await client.get("/api/board/posts/liked")
            self.assertEqual([p["id"] for p in liked.json()["posts"]], ["own1"])

    async def test_relike_is_idempotent_in_liked_list(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        post = dict(id="p1", authorId="u2", authorName="y", title="t", body="b",
                   songRequestUrl=None, createdAt="2026-01-01T00:00:00.000+00:00")
        raw = {"p1": json.dumps(post, ensure_ascii=False)}
        app.state.board = BoardStore(SimpleNamespace(
            hmget=AsyncMock(side_effect=lambda key, ids: [raw.get(i) for i in ids])))
        app.state.post_likes = LikeStore(FakeRedisSet(), "backend:board:post-likes",
                                         "backend:board:user-liked-posts")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            await client.post("/api/board/posts/p1/like")
            await client.post("/api/board/posts/p1/like")  # liking an already-liked post again
            res = await client.get("/api/board/posts/liked")
            body = res.json()
            self.assertEqual(body["total"], 1)
            self.assertEqual(len(body["posts"]), 1)
            self.assertEqual(body["posts"][0]["likeCount"], 1)

    async def test_two_users_liking_same_post_have_independent_liked_lists(self):
        app = create_app(Settings("127.0.0.1", 3000))
        post_a = dict(id="shared", authorId="u3", authorName="z", title="t", body="b",
                     songRequestUrl=None, createdAt="2026-01-01T00:00:00.000+00:00")
        raw = {"shared": json.dumps(post_a, ensure_ascii=False)}
        app.state.board = BoardStore(SimpleNamespace(
            hmget=AsyncMock(side_effect=lambda key, ids: [raw.get(i) for i in ids])))
        app.state.post_likes = LikeStore(FakeRedisSet(), "backend:board:post-likes",
                                         "backend:board:user-liked-posts")
        await app.state.post_likes.like("shared", "u1")

        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts/liked")
            self.assertEqual(res.json()["total"], 1)

        app.dependency_overrides[get_current_user] = lambda: {"id": "u2", "name": "y"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts/liked")
            self.assertEqual(res.json(), {"posts": [], "total": 0})


class LikeApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_like_and_unlike_post_requires_login(self):
        app = create_app(Settings("127.0.0.1", 3000))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            self.assertEqual((await client.post("/api/board/posts/p1/like")).status_code, 401)
            self.assertEqual((await client.delete("/api/board/posts/p1/like")).status_code, 401)

    async def test_like_and_unlike_post(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        app.state.post_likes = LikeStore(FakeRedisSet(), "backend:board:post-likes")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/board/posts/p1/like")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), {"likeCount": 1, "liked": True})

            res = await client.delete("/api/board/posts/p1/like")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), {"likeCount": 0, "liked": False})

    async def test_like_and_unlike_comment(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        app.state.comment_likes = LikeStore(FakeRedisSet(), "backend:board:comment-likes")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/board/comments/c1/like")
            self.assertEqual(res.json(), {"likeCount": 1, "liked": True})
            res = await client.delete("/api/board/comments/c1/like")
            self.assertEqual(res.json(), {"likeCount": 0, "liked": False})

    async def test_like_handles_redis_failure(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        failing = SimpleNamespace(sadd=AsyncMock(side_effect=ConnectionError("offline")))
        app.state.post_likes = LikeStore(failing, "backend:board:post-likes")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/board/posts/p1/like")
            self.assertEqual(res.status_code, 503)


class BoardApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_requires_auth_validates_input_and_returns_post(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "라디오러버"}
        app.state.board = BoardStore(SimpleNamespace(hset=AsyncMock()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/board/posts", json={"title": "제목", "body": "사연 내용"})
            self.assertEqual(res.status_code, 201)
            self.assertEqual(res.json()["post"]["authorName"], "라디오러버")

            res = await client.post("/api/board/posts", json={"title": "", "body": "사연 내용"})
            self.assertEqual(res.status_code, 422)

            res = await client.post("/api/board/posts",
                json={"title": "제목", "body": "사연 내용", "songRequestUrl": "not-a-url"})
            self.assertEqual(res.status_code, 422)

            res = await client.post("/api/board/posts",
                json={"title": "제목", "body": "사연 내용", "songRequestUrl": "https://youtube.com/watch?v=abc"})
            self.assertEqual(res.status_code, 201)

    async def test_create_requires_login(self):
        app = create_app(Settings("127.0.0.1", 3000))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/board/posts", json={"title": "제목", "body": "사연 내용"})
            self.assertEqual(res.status_code, 401)

    async def test_list_paginates_and_reports_total(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "라디오러버"}
        raw = {}
        for i in range(5):
            post = dict(id=str(i), authorId="u1", authorName="x", title=f"t{i}", body="b",
                       songRequestUrl=None, createdAt=f"2026-01-0{i + 1}T00:00:00.000+00:00")
            raw[post["id"]] = json.dumps(post, ensure_ascii=False)
        app.state.board = BoardStore(SimpleNamespace(hgetall=AsyncMock(return_value=raw)))
        app.state.post_likes = LikeStore(FakeRedisSet(), "backend:board:post-likes")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts", params={"offset": 0, "limit": 2})
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["total"], 5)
            self.assertEqual([p["id"] for p in body["posts"]], ["4", "3"])
            self.assertEqual(body["posts"][0]["likeCount"], 0)
            self.assertFalse(body["posts"][0]["liked"])

            res = await client.get("/api/board/posts", params={"offset": 4, "limit": 2})
            self.assertEqual([p["id"] for p in res.json()["posts"]], ["0"])

    async def test_list_handles_redis_failure(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "라디오러버"}
        app.state.board = BoardStore(SimpleNamespace(hgetall=AsyncMock(side_effect=ConnectionError("offline"))))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts")
            self.assertEqual(res.status_code, 503)


class GetPostApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_post_with_like_status(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        post = dict(id="post1", authorId="author1", authorName="글쓴이", title="t", body="b",
                    songRequestUrl=None, createdAt="2026-01-01T00:00:00.000+00:00")
        app.state.board = BoardStore(SimpleNamespace(hget=AsyncMock(return_value=json.dumps(post, ensure_ascii=False))))
        app.state.post_likes = LikeStore(FakeRedisSet(), "backend:board:post-likes")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts/post1")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["post"]["id"], "post1")
            self.assertEqual(res.json()["post"]["likeCount"], 0)

    async def test_missing_post_is_404(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        app.state.board = BoardStore(SimpleNamespace(hget=AsyncMock(return_value=None)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts/missing")
            self.assertEqual(res.status_code, 404)

    async def test_requires_login(self):
        app = create_app(Settings("127.0.0.1", 3000))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts/post1")
            self.assertEqual(res.status_code, 401)

    async def test_mine_and_liked_routes_are_not_shadowed_by_the_dynamic_route(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        app.state.board = BoardStore(SimpleNamespace(hgetall=AsyncMock(return_value={})))
        app.state.post_likes = LikeStore(FakeRedisSet(), "backend:board:post-likes")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts/mine")
            self.assertEqual(res.status_code, 200)
            res = await client.get("/api/board/posts/liked")
            self.assertEqual(res.status_code, 200)


class CommentApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_requires_auth_validates_and_returns_comment(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "별밤지기"}
        redis = FakeRedisList({})
        redis.rpush = AsyncMock(side_effect=lambda key, value: redis.lists.setdefault(key, []).append(value))
        app.state.board_comments = CommentStore(redis)
        app.state.board = BoardStore(SimpleNamespace(hget=AsyncMock(return_value=None)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/board/posts/post1/comments", json={"body": "힘내세요"})
            self.assertEqual(res.status_code, 201)
            self.assertEqual(res.json()["comment"]["authorName"], "별밤지기")
            self.assertEqual(res.json()["comment"]["postId"], "post1")

            res = await client.post("/api/board/posts/post1/comments", json={"body": ""})
            self.assertEqual(res.status_code, 422)

    async def test_create_requires_login(self):
        app = create_app(Settings("127.0.0.1", 3000))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/board/posts/post1/comments", json={"body": "힘내세요"})
            self.assertEqual(res.status_code, 401)

    async def test_create_notifies_post_author_when_commenter_differs(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "commenter1", "name": "댓글러"}
        redis = FakeRedisList({})
        redis.rpush = AsyncMock(side_effect=lambda key, value: redis.lists.setdefault(key, []).append(value))
        app.state.board_comments = CommentStore(redis)
        post = dict(id="post1", authorId="author1", authorName="글쓴이", title="t", body="b",
                    songRequestUrl=None, createdAt="2026-01-01T00:00:00.000+00:00")
        app.state.board = BoardStore(SimpleNamespace(hget=AsyncMock(return_value=json.dumps(post, ensure_ascii=False))))
        app.state.notifications = SimpleNamespace(create=AsyncMock())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/board/posts/post1/comments", json={"body": "힘내세요"})
            self.assertEqual(res.status_code, 201)
        app.state.notifications.create.assert_awaited_once()
        self.assertEqual(app.state.notifications.create.await_args.args[0], "author1")
        self.assertEqual(app.state.notifications.create.await_args.kwargs["data"], {"postId": "post1"})

    async def test_create_skips_notification_when_commenting_on_own_post(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "author1", "name": "글쓴이"}
        redis = FakeRedisList({})
        redis.rpush = AsyncMock(side_effect=lambda key, value: redis.lists.setdefault(key, []).append(value))
        app.state.board_comments = CommentStore(redis)
        post = dict(id="post1", authorId="author1", authorName="글쓴이", title="t", body="b",
                    songRequestUrl=None, createdAt="2026-01-01T00:00:00.000+00:00")
        app.state.board = BoardStore(SimpleNamespace(hget=AsyncMock(return_value=json.dumps(post, ensure_ascii=False))))
        app.state.notifications = SimpleNamespace(create=AsyncMock())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.post("/api/board/posts/post1/comments", json={"body": "내 글에 내 댓글"})
            self.assertEqual(res.status_code, 201)
        app.state.notifications.create.assert_not_awaited()

    async def test_list_paginates_and_handles_redis_failure(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "x"}
        app.state.board_comments = CommentStore(FakeRedisList({}))
        app.state.comment_likes = LikeStore(FakeRedisSet(), "backend:board:comment-likes")
        redis = app.state.board_comments.redis
        redis.lists[app.state.board_comments.key("post1")] = [
            json.dumps(dict(id=str(i), postId="post1", authorId="u1", authorName="x", body=f"c{i}",
                            createdAt="2026-01-01T00:00:00.000+00:00"), ensure_ascii=False)
            for i in range(3)
        ]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            res = await client.get("/api/board/posts/post1/comments", params={"offset": 1, "limit": 1})
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["total"], 3)
            self.assertEqual([c["body"] for c in body["comments"]], ["c1"])

            redis.pipeline = lambda transaction=True: (_ for _ in ()).throw(ConnectionError("offline"))
            res = await client.get("/api/board/posts/post1/comments")
            self.assertEqual(res.status_code, 503)


if __name__ == "__main__":
    unittest.main()
