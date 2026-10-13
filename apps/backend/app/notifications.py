"""Redis-backed per-user notifications. Mirrors the storage pattern used by LikeStore/BoardStore."""
from datetime import datetime, timezone
from typing import Annotated
import asyncio
import json
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.exceptions import RedisError

from .auth import get_current_user

router = APIRouter(prefix="/api/notifications")
User = Annotated[dict, Depends(get_current_user)]
Offset = Annotated[int, Query(ge=0)]


class NotificationStore:
    def __init__(self, redis):
        self.redis = redis

    def records_key(self, user_id: str) -> str:
        return f"backend:notifications:{user_id}"

    def index_key(self, user_id: str) -> str:
        return f"backend:notifications:{user_id}:index"

    def unread_key(self, user_id: str) -> str:
        return f"backend:notifications:{user_id}:unread"

    async def create(self, user_id: str, kind: str, title: str, description: str,
                      data: dict | None = None) -> dict:
        entry = dict(id=str(uuid4()), kind=kind, title=title, description=description, data=data or {},
                     createdAt=datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(self.records_key(user_id), entry["id"], json.dumps(entry, ensure_ascii=False))
            pipe.zadd(self.index_key(user_id), {entry["id"]: time.time()})
            pipe.sadd(self.unread_key(user_id), entry["id"])
            await pipe.execute()
        return entry

    async def list(self, user_id: str, offset: int, limit: int) -> tuple[list[dict], int]:
        index = self.index_key(user_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zrevrange(index, offset, offset + limit - 1)
            pipe.zcard(index)
            ids, total = await pipe.execute()
        if not ids:
            return [], total
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hmget(self.records_key(user_id), ids)
            pipe.smembers(self.unread_key(user_id))
            raw, unread_ids = await pipe.execute()
        entries = [{**json.loads(value), "isUnread": notification_id in unread_ids}
                   for notification_id, value in zip(ids, raw) if value]
        return entries, total

    async def unread_count(self, user_id: str) -> int:
        return await self.redis.scard(self.unread_key(user_id))

    async def mark_read(self, user_id: str, notification_id: str) -> None:
        await self.redis.srem(self.unread_key(user_id), notification_id)

    async def mark_all_read(self, user_id: str) -> None:
        await self.redis.delete(self.unread_key(user_id))

    async def delete(self, user_id: str, notification_id: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hdel(self.records_key(user_id), notification_id)
            pipe.zrem(self.index_key(user_id), notification_id)
            pipe.srem(self.unread_key(user_id), notification_id)
            await pipe.execute()

    async def delete_all(self, user_id: str) -> None:
        await self.redis.delete(self.records_key(user_id))
        await self.redis.delete(self.index_key(user_id))
        await self.redis.delete(self.unread_key(user_id))


@router.get("")
async def list_notifications(request: Request, current_user: User, offset: Offset = 0,
                              limit: Annotated[int, Query(ge=1, le=100)] = 20):
    try:
        async with asyncio.timeout(3):
            notifications, total = await request.app.state.notifications.list(current_user["id"], offset, limit)
            unread_count = await request.app.state.notifications.unread_count(current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"notifications": notifications, "total": total, "unreadCount": unread_count}


@router.post("/{notification_id}/read")
async def mark_notification_read(notification_id: str, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            await request.app.state.notifications.mark_read(current_user["id"], notification_id)
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"ok": True}


@router.post("/read-all")
async def mark_all_notifications_read(request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            await request.app.state.notifications.mark_all_read(current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"ok": True}


@router.delete("/{notification_id}")
async def delete_notification(notification_id: str, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            await request.app.state.notifications.delete(current_user["id"], notification_id)
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"ok": True}


@router.delete("")
async def delete_all_notifications(request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            await request.app.state.notifications.delete_all(current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"ok": True}
