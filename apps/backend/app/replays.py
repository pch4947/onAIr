"""Redis 기반 방송 다시듣기(VOD) 세션 및 북마크. board.py의 저장 패턴을 그대로 따른다.

"세션"은 HlsPublisher 프로세스 하나의 생애주기와 같다(streaming/hls.py의 self.session 참고) —
방송 종료 정리 시 세션을 닫고, 다음 방송에는 새 publisher와 세션을 생성한다.
종료된 세션의 ownerId는 다음 방송 소유자가 바뀌어도 유지한다.
"""
from datetime import datetime, timezone
from typing import Annotated
import asyncio
import json
import shutil

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from redis.exceptions import RedisError

from .auth import get_current_user
from .station_runtime import default_runtime, ensure_runtimes, station_runtime

router = APIRouter(prefix="/api")
User = Annotated[dict, Depends(get_current_user)]
Offset = Annotated[int, Query(ge=0)]
# 세션 ID → 채널. 세션 ID만 있는 경로(북마크, 삭제)가 이것으로 채널을 찾는다
REPLAY_STATIONS_KEY = "backend:replay-stations"


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
            pipe.hset(REPLAY_STATIONS_KEY, session_id, self.station_id)
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
            pipe.hdel(REPLAY_STATIONS_KEY, session_id)
            await pipe.execute()


def public_replay(record: dict, bookmark_count: int, bookmarked: bool) -> dict:
    started = datetime.fromisoformat(record["startedAt"])
    ended = datetime.fromisoformat(record["endedAt"]) if record["endedAt"] else datetime.now(timezone.utc)
    duration = max(0, (ended - started).total_seconds())
    return {
        "id": record["id"], "stationId": record["stationId"], "startedAt": record["startedAt"], "endedAt": record["endedAt"],
        "durationSeconds": round(duration), "ongoing": record["endedAt"] is None,
        "vodUrl": f'/hls/{record["stationId"]}/vod/{record["id"]}/index.m3u8',
        "bookmarkCount": bookmark_count, "bookmarked": bookmarked,
    }


async def is_station_owner(request: Request, station, user_id: str) -> bool:
    raw = await request.app.state.redis.get(station.stations.key)
    return bool(raw and json.loads(raw)["owner_id"] == user_id)


@router.get("/stations/{station_id}/replays")
async def list_replays(station_id: str, request: Request, current_user: User,
                       offset: Offset = 0, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    station = station_runtime(request, station_id)
    try:
        async with asyncio.timeout(3):
            sessions, total = await station.replay_sessions.list(offset, limit)
            marks = await station.replay_bookmarks.status_many(
                [s["id"] for s in sessions], current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"replays": [public_replay(s, *marks[s["id"]]) for s in sessions], "total": total}


async def replay_station(request: Request, session_id: str):
    """세션이 있는 채널. 색인 전에 만든 세션은 채널마다 찾아보고, 어디에도 없으면 기본 채널."""
    runtimes = ensure_runtimes(request.app.state)
    indexed = await request.app.state.redis.hget(REPLAY_STATIONS_KEY, session_id)
    if indexed in runtimes:
        return runtimes[indexed]
    for station in runtimes.values():
        if await request.app.state.redis.hget(station.replay_sessions.prefix + ":replay-sessions", session_id):
            return station
    return default_runtime(request.app)


async def owned_sessions(request: Request, station, user_id: str) -> list[dict]:
    legacy_owner = await is_station_owner(request, station, user_id)
    sessions, _ = await station.replay_sessions.list(0, 2147483647)
    return [s for s in sessions if s.get('ownerId') == user_id or ('ownerId' not in s and legacy_owner)]


async def with_marks(pairs, user_id: str) -> list[dict]:
    """(채널, 세션) 순서를 지키면서 채널별 북마크 상태를 붙인다."""
    marks = {}
    for station in {id(st): st for st, _ in pairs}.values():
        ids = [s["id"] for st, s in pairs if st is station]
        marks.update(await station.replay_bookmarks.status_many(ids, user_id))
    return [public_replay(s, *marks[s["id"]]) for _, s in pairs]


@router.get("/stations/{station_id}/replays/mine")
async def list_my_replays(station_id: str, request: Request, current_user: User,
                          offset: Offset = 0, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    station = station_runtime(request, station_id)
    try:
        async with asyncio.timeout(3):
            owned = await owned_sessions(request, station, current_user['id'])
            sessions, total = owned[offset:offset + limit], len(owned)
            marks = await station.replay_bookmarks.status_many(
                [s["id"] for s in sessions], current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"replays": [public_replay(s, *marks[s["id"]]) for s in sessions], "total": total}


@router.get("/replays/mine")
async def list_all_my_replays(request: Request, current_user: User,
                              offset: Offset = 0, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    """내가 진행한 방송의 다시듣기를 모든 채널에서 모아 최신순으로."""
    try:
        async with asyncio.timeout(3):
            owned = [(station, s) for station in ensure_runtimes(request.app.state).values()
                     for s in await owned_sessions(request, station, current_user['id'])]
            owned.sort(key=lambda pair: (pair[1]["startedAt"], pair[1]["id"]), reverse=True)
            replays = await with_marks(owned[offset:offset + limit], current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"replays": replays, "total": len(owned)}


@router.get("/replays/bookmarked")
async def list_bookmarked_replays(request: Request, current_user: User,
                                  offset: Offset = 0, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    """모든 채널에서 북마크한 다시듣기, 북마크한 순서의 최신순."""
    user_id = current_user["id"]
    try:
        async with asyncio.timeout(3):
            ranked, total = [], 0
            for station in ensure_runtimes(request.app.state).values():
                store = station.replay_bookmarks
                async with store.redis.pipeline(transaction=True) as pipe:
                    pipe.zrevrange(store.user_index_key(user_id), 0, offset + limit - 1, withscores=True)
                    pipe.zcard(store.user_index_key(user_id))
                    entries, count = await pipe.execute()
                ranked += [(score, session_id, station) for session_id, score in entries]
                total += count
            ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
            page = ranked[offset:offset + limit]
            records = {}
            for station in {id(st): st for _, _, st in page}.values():
                records.update(await station.replay_sessions.get_many(
                    [session_id for _, session_id, st in page if st is station]))
            replays = await with_marks([(st, records[session_id]) for _, session_id, st in page
                                        if session_id in records], user_id)
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"replays": replays, "total": total}


@router.post("/replays/{session_id}/bookmark")
async def bookmark_replay(session_id: str, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            bookmarks = (await replay_station(request, session_id)).replay_bookmarks
            await bookmarks.like(session_id, current_user["id"])
            count, bookmarked = await bookmarks.status(session_id, current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"bookmarkCount": count, "bookmarked": bookmarked}


@router.delete("/replays/{session_id}/bookmark")
async def unbookmark_replay(session_id: str, request: Request, current_user: User):
    try:
        async with asyncio.timeout(3):
            bookmarks = (await replay_station(request, session_id)).replay_bookmarks
            await bookmarks.unlike(session_id, current_user["id"])
            count, bookmarked = await bookmarks.status(session_id, current_user["id"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    return {"bookmarkCount": count, "bookmarked": bookmarked}


@router.delete("/replays/{session_id}", status_code=204)
async def delete_replay(session_id: str, request: Request, current_user: User):
    """방송 소유자만 자신의 다시듣기 녹음을 영구 삭제할 수 있다. 진행 중인(아직 안 끝난) 세션은
    HlsPublisher가 지금도 그 vod 디렉터리에 쓰고 있을 수 있어 삭제를 막는다."""
    try:
        async with asyncio.timeout(3):
            station = await replay_station(request, session_id)
            record = (await station.replay_sessions.get_many([session_id])).get(session_id)
            if not record:
                raise HTTPException(404, "다시듣기를 찾을 수 없어요")
            owner = (record['ownerId'] == current_user['id'] if 'ownerId' in record
                     else await is_station_owner(request, station, current_user['id']))
            if not owner:
                raise HTTPException(403, "본인 방송만 삭제할 수 있어요")
            if record["endedAt"] is None:
                raise HTTPException(409, "진행 중인 방송은 삭제할 수 없어요")
            # 삭제된 세션이 북마크한 사람들의 목록/카운트에 고아로 남지 않도록 같이 정리한다.
            bookmarks = station.replay_bookmarks
            bookmarker_ids = await request.app.state.redis.smembers(bookmarks.key(session_id))
            for user_id in bookmarker_ids:
                await request.app.state.redis.zrem(bookmarks.user_index_key(user_id), session_id)
            await request.app.state.redis.delete(bookmarks.key(session_id))
            await station.replay_sessions.delete(session_id)
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    vod_dir = station.hls.directory / "vod" / session_id
    shutil.rmtree(vod_dir, ignore_errors=True)
