"""Redis 기반 방송 다시듣기(VOD) 세션 및 북마크. board.py의 저장 패턴을 그대로 따른다.

"세션"은 HlsPublisher 프로세스 하나의 생애주기와 같다(streaming/hls.py의 self.session 참고) —
단일 station 프로토타입에는 아직 별도의 방송 시작/종료 API가 없어서, 프로세스 생애주기가
지금 쓸 수 있는 가장 가까운 세션 경계다. 실제 세션 id가 생기면 이걸로 교체하면 된다.
"""
from datetime import datetime, timezone
from typing import Annotated
import asyncio
import json
import shutil

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.exceptions import RedisError

from .auth import get_current_user

router = APIRouter(prefix="/api")
User = Annotated[dict, Depends(get_current_user)]
Offset = Annotated[int, Query(ge=0)]


def iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat(timespec="milliseconds")


class ReplaySessionStore:
    def __init__(self, redis, station_id):
        self.redis = redis
        self.station_id = station_id
        self.prefix = f"backend:{station_id}"

    async def start(self, session_id: str, started_at: float) -> None:
        record = dict(id=session_id, stationId=self.station_id, startedAt=iso(started_at), endedAt=None)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(self.prefix + ":replay-sessions", session_id, json.dumps(record))
            pipe.zadd(self.prefix + ":replay-index", {session_id: started_at})
            await pipe.execute()

    async def end(self, session_id: str, ended_at: float) -> None:
        raw = await self.redis.hget(self.prefix + ":replay-sessions", session_id)
        if not raw:
            return
        record = json.loads(raw)
        record["endedAt"] = iso(ended_at)
        await self.redis.hset(self.prefix + ":replay-sessions", session_id, json.dumps(record))

    async def get_many(self, session_ids: list[str]) -> dict[str, dict]:
        if not session_ids:
            return {}
        raws = await self.redis.hmget(self.prefix + ":replay-sessions", session_ids)
        return {sid: json.loads(raw) for sid, raw in zip(session_ids, raws) if raw}

    async def list(self, offset: int, limit: int) -> tuple[list[dict], int]:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zrevrange(self.prefix + ":replay-index", offset, offset + limit - 1)
            pipe.zcard(self.prefix + ":replay-index")
            ids, total = await pipe.execute()
        by_id = await self.get_many(ids)
        return [by_id[sid] for sid in ids if sid in by_id], total

    async def delete(self, session_id: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hdel(self.prefix + ":replay-sessions", session_id)
            pipe.zrem(self.prefix + ":replay-index", session_id)
            await pipe.execute()


def public_replay(record: dict, bookmark_count: int, bookmarked: bool) -> dict:
    started = datetime.fromisoformat(record["startedAt"])
    ended = datetime.fromisoformat(record["endedAt"]) if record["endedAt"] else datetime.now(timezone.utc)
    duration = max(0, (ended - started).total_seconds())
    return {
        "id": record["id"], "startedAt": record["startedAt"], "endedAt": record["endedAt"],
        "durationSeconds": round(duration), "ongoing": record["endedAt"] is None,
        "vodUrl": f'/hls/{record["stationId"]}/vod/{record["id"]}/index.m3u8',
        "bookmarkCount": bookmark_count, "bookmarked": bookmarked,
    }


def require_station(station_id: str, request: Request) -> None:
    if station_id != request.app.state.settings.station_id:
        raise HTTPException(404, "Station not found")


async def is_station_owner(request: Request, user_id: str) -> bool:
    raw = await request.app.state.redis.get(request.app.state.stations.key)
    return bool(raw and json.loads(raw)["owner_id"] == user_id)


@router.get("/stations/{station_id}/replays")
async def list_replays(station_id: str, request: Request, current_user: User,
                       offset: Offset = 0, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    require_station(station_id, request)
    try:
        async with asyncio.timeout(3):
            sessions, total = await request.app.state.replay_sessions.list(offset, limit)
            marks = await request.app.state.replay_bookmarks.status_many(
                [s["id"] for s in sessions], current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"replays": [public_replay(s, *marks[s["id"]]) for s in sessions], "total": total}


@router.get("/stations/{station_id}/replays/mine")
async def list_my_replays(station_id: str, request: Request, current_user: User,
                          offset: Offset = 0, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    require_station(station_id, request)
    try:
        async with asyncio.timeout(3):
            if not await is_station_owner(request, current_user["id"]):
                return {"replays": [], "total": 0}
            sessions, total = await request.app.state.replay_sessions.list(offset, limit)
            marks = await request.app.state.replay_bookmarks.status_many(
                [s["id"] for s in sessions], current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"replays": [public_replay(s, *marks[s["id"]]) for s in sessions], "total": total}


@router.get("/replays/bookmarked")
async def list_bookmarked_replays(request: Request, current_user: User,
                                  offset: Offset = 0, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    try:
        async with asyncio.timeout(3):
            session_ids, total = await request.app.state.replay_bookmarks.liked_by(
                current_user["id"], offset, limit)
            by_id = await request.app.state.replay_sessions.get_many(session_ids)
            marks = await request.app.state.replay_bookmarks.status_many(session_ids, current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    replays = [public_replay(by_id[sid], *marks[sid]) for sid in session_ids if sid in by_id]
    return {"replays": replays, "total": total}


@router.post("/replays/{session_id}/bookmark")
async def bookmark_replay(session_id: str, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            await request.app.state.replay_bookmarks.like(session_id, current_user["id"])
            count, bookmarked = await request.app.state.replay_bookmarks.status(session_id, current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"bookmarkCount": count, "bookmarked": bookmarked}


@router.delete("/replays/{session_id}/bookmark")
async def unbookmark_replay(session_id: str, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            await request.app.state.replay_bookmarks.unlike(session_id, current_user["id"])
            count, bookmarked = await request.app.state.replay_bookmarks.status(session_id, current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"bookmarkCount": count, "bookmarked": bookmarked}


@router.delete("/replays/{session_id}", status_code=204)
async def delete_replay(session_id: str, request: Request, current_user: User):
    """방송 소유자만 자신의 다시듣기 녹음을 영구 삭제할 수 있다. 진행 중인(아직 안 끝난) 세션은
    HlsPublisher가 지금도 그 vod 디렉터리에 쓰고 있을 수 있어 삭제를 막는다."""
    try:
        async with asyncio.timeout(3):
            if not await is_station_owner(request, current_user["id"]):
                raise HTTPException(403, "본인 방송만 삭제할 수 있어요")
            record = (await request.app.state.replay_sessions.get_many([session_id])).get(session_id)
            if not record:
                raise HTTPException(404, "다시듣기를 찾을 수 없어요")
            if record["endedAt"] is None:
                raise HTTPException(409, "진행 중인 방송은 삭제할 수 없어요")
            # 삭제된 세션이 북마크한 사람들의 목록/카운트에 고아로 남지 않도록 같이 정리한다.
            bookmarks = request.app.state.replay_bookmarks
            bookmarker_ids = await request.app.state.redis.smembers(bookmarks.key(session_id))
            for user_id in bookmarker_ids:
                await request.app.state.redis.zrem(bookmarks.user_index_key(user_id), session_id)
            await request.app.state.redis.delete(bookmarks.key(session_id))
            await request.app.state.replay_sessions.delete(session_id)
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    vod_dir = request.app.state.hls.directory / "vod" / session_id
    shutil.rmtree(vod_dir, ignore_errors=True)
