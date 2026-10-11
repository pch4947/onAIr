"""Board post/comment storage and API — auth, validation, pagination, and Redis failure handling."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

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

    async def execute(self):
        results = []
        for call in self.calls:
            if call[0] == "scard":
                results.append(len(self.redis.sets.get(call[1], set())))
            else:
                results.append(call[2] in self.redis.sets.get(call[1], set()))
        return results


class FakeRedisSet:
    def __init__(self, sets=None):
        self.sets = sets if sets is not None else {}

    async def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    async def srem(self, key, member):
        self.sets.setdefault(key, set()).discard(member)

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


class CommentApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_requires_auth_validates_and_returns_comment(self):
        app = create_app(Settings("127.0.0.1", 3000))
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "name": "별밤지기"}
        redis = FakeRedisList({})
        redis.rpush = AsyncMock(side_effect=lambda key, value: redis.lists.setdefault(key, []).append(value))
        app.state.board_comments = CommentStore(redis)
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
