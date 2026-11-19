"""계정 설정 (#325): 닉네임·비밀번호 변경, 기본 익명, 탈퇴. 일반 회원 계정만 쓴다.

닉네임은 게시글·댓글·요청 기록에 복사돼 있어 바꾸면 그 이름도 함께 바꾼다(방송에 이미 나간 내용은 그대로).
탈퇴하면 그 이름을 '탈퇴한 사용자'로 바꿔 남기고, 진행 중인 내 방송이 있으면 탈퇴를 막는다.
"""
import asyncio
import json
import time
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from redis.exceptions import RedisError

from .auth import JWT_ALGORITHM, get_current_user, public_user, reject_reserved_name
from .board_stories import POSTS_KEY
from .notifications import NotificationStore
from .station_runtime import ensure_runtimes
from .stations import channel_records
from .users import WITHDRAWN_NAME, NameAlreadyExists, UserChanged, hash_password, verify_password

router = APIRouter(prefix='/api/account')
User = Annotated[dict, Depends(get_current_user)]

COMMENTS_PATTERN = 'backend:board:comments:*'
# 읽은 값 그대로일 때만 바꾼다 — 그사이 다른 요청이 고친 항목은 다음 바퀴에서 다시 읽는다
REPLACE_HASH = "if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return 0 end " \
               "redis.call('HSET', KEYS[1], ARGV[1], ARGV[3]) return 1"
REPLACE_LIST = "if redis.call('LINDEX', KEYS[1], tonumber(ARGV[1])) ~= ARGV[2] then return 0 end " \
               "redis.call('LSET', KEYS[1], tonumber(ARGV[1]), ARGV[3]) return 1"
REWRITE_PASSES = 3


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
async def change_password(body: PasswordInput, request: Request, user: User,
                          authorization: Annotated[str | None, Header()] = None):
    """바꾸면 그 전에 발급된 토큰은 모두 버린다(다른 기기는 다시 로그인). 이 요청에 쓴 토큰만 그대로 쓸 수 있다 (#344)."""
    require_member(user)
    check_password(user, body.currentPassword)
    new_hash = hash_password(body.newPassword)
    # get_current_user가 방금 검증한 토큰이다
    kept = jwt.decode((authorization or '').removeprefix('Bearer ').strip(),
                      request.app.state.settings.jwt_secret, algorithms=[JWT_ALGORITHM]).get('iat')
    try:
        async with asyncio.timeout(3):
            await request.app.state.users.update(user['email'], lambda u: dict(
                u, passwordHash=new_hash, passwordChangedAt=time.time(), keptTokenIat=kept))
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
