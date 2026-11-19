"""계정 설정 (#325): 닉네임·비밀번호 변경, 기본 익명, 내가 보낸 것, 탈퇴. 일반 회원 계정만 쓴다.

닉네임은 게시글·댓글·요청 기록에 복사돼 있어 바꾸면 그 이름도 함께 바꾼다(방송에 이미 나간 내용은 그대로).
탈퇴하면 그 이름을 '탈퇴한 사용자'로 바꿔 남기고, 진행 중인 내 방송이 있으면 탈퇴를 막는다.
"""
import asyncio
from datetime import datetime
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from redis.exceptions import RedisError

from .auth import get_current_user, public_user, reject_reserved_name
from .board_stories import POSTS_KEY
from .notifications import NotificationStore
from .requests import RequestStore
from .station_runtime import ensure_runtimes
from .stations import channel_records
from .users import NameAlreadyExists, UserChanged, hash_password, verify_password
from .votes import vote_key

router = APIRouter(prefix='/api/account')
User = Annotated[dict, Depends(get_current_user)]

WITHDRAWN_NAME = '탈퇴한 사용자'
COMMENTS_PATTERN = 'backend:board:comments:*'
# 읽은 값 그대로일 때만 바꾼다 — 그사이 다른 요청이 고친 항목은 다음 바퀴에서 다시 읽는다
REPLACE_HASH = "if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return 0 end " \
               "redis.call('HSET', KEYS[1], ARGV[1], ARGV[3]) return 1"
REPLACE_LIST = "if redis.call('LINDEX', KEYS[1], tonumber(ARGV[1])) ~= ARGV[2] then return 0 end " \
               "redis.call('LSET', KEYS[1], tonumber(ARGV[1]), ARGV[3]) return 1"
REWRITE_PASSES = 3
SENT_PAGE_LIMIT = 50


class NameInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=50, pattern=r'\S')


class PasswordInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    currentPassword: str = Field(min_length=1, max_length=128)
    newPassword: str = Field(min_length=8, max_length=128)


class PreferencesInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    defaultAnonymous: StrictBool


class DeleteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: str = Field(min_length=1, max_length=128)


def require_member(user):
    # 게스트는 로그아웃하면 계정이 지워지고, 어드민은 핀으로 들어오는 공용 계정이다
    if user.get('kind', 'member') != 'member':
        raise HTTPException(403, 'Only member accounts have account settings')


def check_password(user, password):
    if not user.get('passwordHash') or not verify_password(password, user['passwordHash']):
        raise HTTPException(403, 'Password does not match')


async def rewrite_hash(redis, key, match, change):
    for _ in range(REWRITE_PASSES):
        missed = 0
        for field, raw in (await redis.hgetall(key)).items():
            entry = json.loads(raw)
            if match(entry):
                missed += not await redis.eval(REPLACE_HASH, 1, key, field, raw,
                                               json.dumps(change(entry), ensure_ascii=False))
        if not missed:
            return


async def rewrite_list(redis, key, match, change):
    for _ in range(REWRITE_PASSES):
        missed = 0
        for index, raw in enumerate(await redis.lrange(key, 0, -1)):
            entry = json.loads(raw)
            if match(entry):
                missed += not await redis.eval(REPLACE_LIST, 1, key, str(index), raw,
                                               json.dumps(change(entry), ensure_ascii=False))
        if not missed:
            return


async def rename_everywhere(request, user_id, name):
    """게시글·댓글과 모든 채널의 요청 기록에서 이 사용자의 이름을 바꾼다. 익명으로 보낸 요청은 그대로 익명이다."""
    redis = request.app.state.redis
    authored = lambda entry: entry.get('authorId') == user_id  # noqa: E731
    await rewrite_hash(redis, POSTS_KEY, authored, lambda entry: dict(entry, authorName=name))
    async for key in redis.scan_iter(match=COMMENTS_PATTERN, _type='list'):
        await rewrite_list(redis, key, authored, lambda entry: dict(entry, authorName=name))
    for runtime in ensure_runtimes(request.app.state).values():
        await rewrite_hash(redis, f'backend:{runtime.station_id}:requests',
                           lambda entry: entry.get('listenerId') == user_id and entry.get('listenerName'),
                           lambda entry: dict(entry, listenerName=name))


@router.post('/name')
async def change_name(body: NameInput, request: Request, user: User):
    require_member(user)
    name = body.name.strip()
    reject_reserved_name(name, request)
    try:
        async with asyncio.timeout(10):
            updated = await request.app.state.users.update(user['email'], lambda u: dict(u, name=name), new_name=name)
            await rename_everywhere(request, user['id'], name)
    except NameAlreadyExists:
        raise HTTPException(409, 'Name already taken') from None
    except UserChanged:
        raise HTTPException(409, 'Account changed; retry') from None
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    return {'user': public_user(updated)}


@router.post('/password', status_code=204)
async def change_password(body: PasswordInput, request: Request, user: User):
    require_member(user)
    check_password(user, body.currentPassword)
    new_hash = hash_password(body.newPassword)
    try:
        async with asyncio.timeout(3):
            await request.app.state.users.update(user['email'], lambda u: dict(u, passwordHash=new_hash))
    except UserChanged:
        raise HTTPException(409, 'Account changed; retry') from None
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    return Response(status_code=204)


@router.post('/preferences')
async def change_preferences(body: PreferencesInput, request: Request, user: User):
    require_member(user)
    try:
        async with asyncio.timeout(3):
            updated = await request.app.state.users.update(
                user['email'], lambda u: dict(u, defaultAnonymous=body.defaultAnonymous))
    except UserChanged:
        raise HTTPException(409, 'Account changed; retry') from None
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    return {'user': public_user(updated)}


async def sent_items(request, user_id):
    redis = request.app.state.redis
    items = []
    for runtime in ensure_runtimes(request.app.state).values():
        sid = runtime.station_id
        async with redis.pipeline(transaction=True) as pipe:
            pipe.hgetall(f'backend:{sid}:requests')
            pipe.hgetall(f'backend:{sid}:request-states')
            pipe.hgetall(vote_key(sid, 'votes'))
            requests, states, votes = await pipe.execute()
        for key, raw in requests.items():
            entry = RequestStore.merge(raw, states.get(key))
            if entry.get('listenerId') != user_id:
                continue
            items.append({'type': 'request', 'id': entry['id'], 'stationId': sid, 'kind': entry['kind'],
                          'prompt': entry['prompt'], 'status': entry['status'], 'anonymous': not entry.get('listenerName'),
                          'trackTitle': entry.get('trackTitle'), 'trackArtist': entry.get('trackArtist'),
                          'createdAt': entry['createdAt']})
        # 투표는 "투표ID:사용자ID" → 고른 것. 내 표만 찾는다
        async for field, choice in redis.hscan_iter(vote_key(sid, 'vote-ballots'), match=f'*:{user_id}'):
            vote_id = field.rsplit(':', 1)[0]
            vote = json.loads(votes[vote_id]) if vote_id in votes else None
            if vote is None:
                continue
            items.append({'type': 'vote', 'id': vote_id, 'stationId': sid, 'question': vote.get('question'),
                          'choice': choice, 'status': vote.get('status'), 'at': float(vote.get('opens_at') or 0)})
    return items


def sort_key(item):
    return item['at'] if item['type'] == 'vote' else datetime.fromisoformat(item['createdAt']).timestamp()


@router.get('/sent')
async def list_sent(request: Request, user: User, offset: Annotated[int, Query(ge=0)] = 0,
                    limit: Annotated[int, Query(ge=1, le=SENT_PAGE_LIMIT)] = 20):
    """내가 보낸 채팅·사연·신청곡과 투표, 최근 것부터. 지난 방송 것도 포함한다."""
    try:
        async with asyncio.timeout(5):
            items = await sent_items(request, user['id'])
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    items.sort(key=sort_key, reverse=True)
    return {'items': items[offset:offset + limit], 'total': len(items)}


@router.delete('', status_code=204)
async def delete_account(body: DeleteInput, request: Request, user: User):
    """비밀번호를 확인하고 지운다. 보낸 글·댓글·요청은 '탈퇴한 사용자'로 남는다. 진행 중인 내 방송이 있으면 409."""
    require_member(user)
    check_password(user, body.password)
    redis = request.app.state.redis
    try:
        async with asyncio.timeout(10):
            live = next((record for _, record in await channel_records(request)
                         if record and record['owner_id'] == user['id'] and record['status'] != 'ended'), None)
            if live:
                raise HTTPException(409, {'message': 'End your broadcast before deleting the account',
                                          'station_id': live['station_id']})
            await rename_everywhere(request, user['id'], WITHDRAWN_NAME)
            # 내가 누른 좋아요는 지운다(수에서도 빠진다). 댓글 좋아요는 사용자별 목록이 없어 수만 남는다
            post_likes = request.app.state.post_likes
            liked = await redis.zrange(post_likes.user_index_key(user['id']), 0, -1)
            for post_id in liked:
                await post_likes.unlike(post_id, user['id'])
            store = NotificationStore(redis)
            await redis.delete(store.records_key(user['id']), store.index_key(user['id']), store.unread_key(user['id']))
            deleted = await request.app.state.users.delete(user)
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    if not deleted:
        raise HTTPException(409, 'Account changed; retry')
    return Response(status_code=204)
