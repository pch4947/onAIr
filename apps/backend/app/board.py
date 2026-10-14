"""Redis-backed board posts, comments, and likes (사연 게시판). Mirrors the storage pattern used by RequestStore.

Engine hand-off (board.story) and topic matching are out of scope here —
see issue tracking for the follow-up that wires posts into the broadcast pipeline.
"""
from datetime import datetime, timezone
from typing import Annotated
import asyncio
import json
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from .auth import get_current_user

router = APIRouter(prefix="/api/board")
User = Annotated[dict, Depends(get_current_user)]
Offset = Annotated[int, Query(ge=0)]


class BoardPostInput(BaseModel):
    title: str = Field(min_length=1, max_length=100, pattern=r"\S")
    body: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    songRequestUrl: str | None = Field(default=None, max_length=500, pattern=r"^https?://\S+$")


class CommentInput(BaseModel):
    body: str = Field(min_length=1, max_length=500, pattern=r"\S")


class BoardStore:
    def __init__(self, redis):
        self.redis = redis
        self.key = "backend:board:posts"

    async def create(self, author_id: str, author_name: str, title: str, body: str,
                     song_request_url: str | None) -> dict:
        entry = dict(id=str(uuid4()), authorId=author_id, authorName=author_name, title=title,
                     body=body, songRequestUrl=song_request_url,
                     createdAt=datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        await self.redis.hset(self.key, entry["id"], json.dumps(entry, ensure_ascii=False))
        return entry

    async def get(self, post_id: str) -> dict | None:
        raw = await self.redis.hget(self.key, post_id)
        return json.loads(raw) if raw else None

    # Defined before `list` below — once that method exists, its name shadows the
    # builtin `list` for annotations evaluated later in this class body.
    async def get_many(self, post_ids: list[str]) -> dict[str, dict]:
        if not post_ids:
            return {}
        raw = await self.redis.hmget(self.key, post_ids)
        return {post_id: json.loads(value) for post_id, value in zip(post_ids, raw) if value}

    async def list(self) -> list[dict]:
        raw = await self.redis.hgetall(self.key)
        posts = [json.loads(value) for value in raw.values()]
        # Newest first, matching the board feed's reading order.
        return sorted(posts, key=lambda entry: (entry["createdAt"], entry["id"]), reverse=True)

    async def delete(self, post_id: str) -> None:
        await self.redis.hdel(self.key, post_id)


class CommentStore:
    def __init__(self, redis):
        self.redis = redis

    def key(self, post_id: str) -> str:
        return f"backend:board:comments:{post_id}"

    async def create(self, post_id: str, author_id: str, author_name: str, body: str) -> dict:
        entry = dict(id=str(uuid4()), postId=post_id, authorId=author_id, authorName=author_name,
                     body=body, createdAt=datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        await self.redis.rpush(self.key(post_id), json.dumps(entry, ensure_ascii=False))
        return entry

    # Defined before `list` below — once that method exists, its name shadows the
    # builtin `list` for annotations evaluated later in this class body.
    async def get_one(self, post_id: str, comment_id: str) -> dict | None:
        raw = await self.redis.lrange(self.key(post_id), 0, -1)
        for item in raw:
            entry = json.loads(item)
            if entry["id"] == comment_id:
                return entry
        return None

    async def delete_one(self, post_id: str, comment_id: str) -> None:
        raw = await self.redis.lrange(self.key(post_id), 0, -1)
        for item in raw:
            if json.loads(item)["id"] == comment_id:
                await self.redis.lrem(self.key(post_id), 1, item)
                return

    async def delete_all(self, post_id: str) -> list[str]:
        """Remove every comment under a post; returns their ids so likes can be cleaned up too."""
        raw = await self.redis.lrange(self.key(post_id), 0, -1)
        await self.redis.delete(self.key(post_id))
        return [json.loads(item)["id"] for item in raw]

    async def list(self, post_id: str, offset: int, limit: int) -> tuple[list[dict], int]:
        key = self.key(post_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.lrange(key, offset, offset + limit - 1)
            pipe.llen(key)
            raw, total = await pipe.execute()
        return [json.loads(item) for item in raw], total


class LikeStore:
    """One Redis SET per liked thing, keyed by the liking user's id — SCARD for count, SISMEMBER for mine.

    When user_index_prefix is set, also maintains a per-user ZSET (member=target_id, score=like time)
    so "things this user liked, most recent first" can be listed without scanning every target.
    Comment likes don't need this view, so comment_likes leaves user_index_prefix unset.
    """

    def __init__(self, redis, prefix: str, user_index_prefix: str | None = None):
        self.redis = redis
        self.prefix = prefix
        self.user_index_prefix = user_index_prefix

    def key(self, target_id: str) -> str:
        return f"{self.prefix}:{target_id}"

    def user_index_key(self, user_id: str) -> str:
        return f"{self.user_index_prefix}:{user_id}"

    async def like(self, target_id: str, user_id: str) -> None:
        await self.redis.sadd(self.key(target_id), user_id)
        if self.user_index_prefix:
            await self.redis.zadd(self.user_index_key(user_id), {target_id: time.time()})

    async def unlike(self, target_id: str, user_id: str) -> None:
        await self.redis.srem(self.key(target_id), user_id)
        if self.user_index_prefix:
            await self.redis.zrem(self.user_index_key(user_id), target_id)

    async def liked_by(self, user_id: str, offset: int, limit: int) -> tuple[list[str], int]:
        """Target ids this user liked, most recently liked first. Requires user_index_prefix."""
        key = self.user_index_key(user_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zrevrange(key, offset, offset + limit - 1)
            pipe.zcard(key)
            ids, total = await pipe.execute()
        return ids, total

    async def status(self, target_id: str, user_id: str) -> tuple[int, bool]:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.scard(self.key(target_id))
            pipe.sismember(self.key(target_id), user_id)
            count, liked = await pipe.execute()
        return count, bool(liked)

    async def delete(self, target_id: str) -> None:
        """Drop a target's like set entirely, also clearing it from any per-user index."""
        key = self.key(target_id)
        if self.user_index_prefix:
            members = await self.redis.smembers(key)
            for member in members:
                await self.redis.zrem(self.user_index_key(member), target_id)
        await self.redis.delete(key)

    async def status_many(self, target_ids: list[str], user_id: str) -> dict[str, tuple[int, bool]]:
        if not target_ids:
            return {}
        async with self.redis.pipeline(transaction=True) as pipe:
            for target_id in target_ids:
                pipe.scard(self.key(target_id))
                pipe.sismember(self.key(target_id), user_id)
            results = await pipe.execute()
        return {target_id: (results[2 * i], bool(results[2 * i + 1]))
                for i, target_id in enumerate(target_ids)}


def with_like_status(entry: dict, count: int, liked: bool) -> dict:
    return {**entry, "likeCount": count, "liked": liked}


@router.post("/posts", status_code=201)
async def create_post(body: BoardPostInput, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            entry = await request.app.state.board.create(
                current_user["id"], current_user["name"], body.title, body.body, body.songRequestUrl)
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"post": with_like_status(entry, 0, False)}


@router.get("/posts")
async def list_posts(request: Request, current_user: User, offset: Offset = 0,
                     limit: Annotated[int, Query(ge=1, le=100)] = 20):
    try:
        async with asyncio.timeout(3):
            posts = await request.app.state.board.list()
            page = posts[offset:offset + limit]
            likes = await request.app.state.post_likes.status_many(
                [post["id"] for post in page], current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    enriched = [with_like_status(post, *likes[post["id"]]) for post in page]
    return {"posts": enriched, "total": len(posts)}


@router.get("/posts/mine")
async def list_my_posts(request: Request, current_user: User, offset: Offset = 0,
                        limit: Annotated[int, Query(ge=1, le=100)] = 20):
    try:
        async with asyncio.timeout(3):
            posts = await request.app.state.board.list()
            mine = [post for post in posts if post["authorId"] == current_user["id"]]
            page = mine[offset:offset + limit]
            likes = await request.app.state.post_likes.status_many(
                [post["id"] for post in page], current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    enriched = [with_like_status(post, *likes[post["id"]]) for post in page]
    return {"posts": enriched, "total": len(mine)}


@router.get("/posts/liked")
async def list_liked_posts(request: Request, current_user: User, offset: Offset = 0,
                           limit: Annotated[int, Query(ge=1, le=100)] = 20):
    try:
        async with asyncio.timeout(3):
            post_ids, total = await request.app.state.post_likes.liked_by(
                current_user["id"], offset, limit)
            posts_by_id = await request.app.state.board.get_many(post_ids)
            likes = await request.app.state.post_likes.status_many(post_ids, current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    enriched = [with_like_status(posts_by_id[pid], *likes[pid]) for pid in post_ids if pid in posts_by_id]
    return {"posts": enriched, "total": total}


@router.get("/posts/{post_id}")
async def get_post(post_id: str, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            post = await request.app.state.board.get(post_id)
            if not post:
                raise HTTPException(404, "Post not found")
            count, liked = await request.app.state.post_likes.status(post_id, current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"post": with_like_status(post, count, liked)}


@router.post("/posts/{post_id}/like")
async def like_post(post_id: str, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            await request.app.state.post_likes.like(post_id, current_user["id"])
            count, liked = await request.app.state.post_likes.status(post_id, current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"likeCount": count, "liked": liked}


@router.delete("/posts/{post_id}", status_code=204)
async def delete_post(post_id: str, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            post = await request.app.state.board.get(post_id)
            if not post:
                raise HTTPException(404, "Post not found")
            if post["authorId"] != current_user["id"]:
                raise HTTPException(403, "Only the author can delete this post")
            comment_ids = await request.app.state.board_comments.delete_all(post_id)
            for comment_id in comment_ids:
                await request.app.state.comment_likes.delete(comment_id)
            await request.app.state.post_likes.delete(post_id)
            await request.app.state.board.delete(post_id)
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return None


@router.delete("/posts/{post_id}/like")
async def unlike_post(post_id: str, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            await request.app.state.post_likes.unlike(post_id, current_user["id"])
            count, liked = await request.app.state.post_likes.status(post_id, current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"likeCount": count, "liked": liked}


@router.post("/posts/{post_id}/comments", status_code=201)
async def create_comment(post_id: str, body: CommentInput, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            entry = await request.app.state.board_comments.create(
                post_id, current_user["id"], current_user["name"], body.body)
            post = await request.app.state.board.get(post_id)
            if post and post["authorId"] != current_user["id"]:
                await request.app.state.notifications.create(
                    post["authorId"], "comment", "새 댓글이 달렸어요",
                    f'{current_user["name"]} 님이 내 글에 댓글을 남겼어요', data={"postId": post_id})
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"comment": with_like_status(entry, 0, False)}


@router.get("/posts/{post_id}/comments")
async def list_comments(post_id: str, request: Request, current_user: User, offset: Offset = 0,
                        limit: Annotated[int, Query(ge=1, le=200)] = 50):
    try:
        async with asyncio.timeout(3):
            comments, total = await request.app.state.board_comments.list(post_id, offset, limit)
            likes = await request.app.state.comment_likes.status_many(
                [comment["id"] for comment in comments], current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    enriched = [with_like_status(comment, *likes[comment["id"]]) for comment in comments]
    return {"comments": enriched, "total": total}


@router.delete("/posts/{post_id}/comments/{comment_id}", status_code=204)
async def delete_comment(post_id: str, comment_id: str, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            comment = await request.app.state.board_comments.get_one(post_id, comment_id)
            if not comment:
                raise HTTPException(404, "Comment not found")
            if comment["authorId"] != current_user["id"]:
                raise HTTPException(403, "Only the author can delete this comment")
            await request.app.state.board_comments.delete_one(post_id, comment_id)
            await request.app.state.comment_likes.delete(comment_id)
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return None


@router.post("/comments/{comment_id}/like")
async def like_comment(comment_id: str, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            await request.app.state.comment_likes.like(comment_id, current_user["id"])
            count, liked = await request.app.state.comment_likes.status(comment_id, current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"likeCount": count, "liked": liked}


@router.delete("/comments/{comment_id}/like")
async def unlike_comment(comment_id: str, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            await request.app.state.comment_likes.unlike(comment_id, current_user["id"])
            count, liked = await request.app.state.comment_likes.status(comment_id, current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"likeCount": count, "liked": liked}
