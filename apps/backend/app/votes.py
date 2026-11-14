"""청취자 투표 (계약 docs/ENGINE_REDIS_CONTRACT.md 10.8절, #279).

엔진이 vote.open을 보내면 기준 세그먼트별로 들고 있다가(vote-pending), 그 세그먼트의 재생이 시작될 때
(segment.started, journal.DELIVER) 연다. window_sec 뒤에 닫아 엔진에 vote.result를 보낸다.
한 청취자 한 표이고, 표마다 엔진에 알리지 않는다. 기준 세그먼트가 버려지면 투표도 열지 않는다.
"""
import asyncio
import json
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from .auth import get_current_user
from .station_runtime import station_runtime

router = APIRouter(prefix='/api/stations')
User = Annotated[dict, Depends(get_current_user)]

# 기준 세그먼트 → 아직 열지 않은 vote.open, vote_id → 투표 기록, 지금(마지막) 투표, 표, 선택지별 표 수
VOTE_KEYS = ('vote-pending', 'votes', 'current-vote', 'vote-ballots', 'vote-counts')
# 닫힌 투표의 결과를 화면에 남겨 두는 시간
RESULT_SHOWN_SECONDS = 60

# 창이 지난 투표를 닫고 결과를 보낸다. 여러 번 불러도 한 번만 보낸다
CLOSE = """
local closed = 0
local all = redis.call('HGETALL', KEYS[1])
for i = 1, #all, 2 do
 local vote = cjson.decode(all[i + 1])
 if vote.status == 'open' and tonumber(vote.closes_at) <= tonumber(ARGV[1]) then
  local counts, total = {}, 0
  for _, option in ipairs(vote.options) do
   local n = tonumber(redis.call('HGET', KEYS[2], vote.vote_id .. '\\0' .. option) or '0')
   counts[option] = n
   total = total + n
  end
  vote.status = 'closed'
  vote.counts = counts
  vote.total = total
  redis.call('HSET', KEYS[1], all[i], cjson.encode(vote))
  redis.call('XADD', KEYS[3], '*', 'type', 'vote.result', 'contract_version', '2',
   'event_id', 'evt_vote_' .. vote.vote_id, 'station_id', ARGV[2], 'at', ARGV[1],
   'payload', cjson.encode({vote_id=vote.vote_id, counts=counts, total=total}))
  closed = closed + 1
 end
end
return closed
"""

# 한 청취자 한 표. 이미 투표했으면 처음 고른 선택지를 돌려준다
BALLOT = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return {'missing', ''} end
local vote = cjson.decode(raw)
local now = tonumber(ARGV[4])
if vote.status ~= 'open' or now < tonumber(vote.opens_at) or now >= tonumber(vote.closes_at) then
 return {'closed', ''}
end
local valid = false
for _, option in ipairs(vote.options) do if option == ARGV[3] then valid = true end end
if not valid then return {'invalid', ''} end
local field = ARGV[1] .. ':' .. ARGV[2]
if redis.call('HSETNX', KEYS[2], field, ARGV[3]) == 0 then
 return {'voted', redis.call('HGET', KEYS[2], field)}
end
redis.call('HINCRBY', KEYS[3], ARGV[1] .. '\\0' .. ARGV[3], 1)
return {'ok', ARGV[3]}
"""


def vote_key(station_id, name):
    return f'backend:{station_id}:{name}'


async def close_due(redis, station_id, now=None):
    """창이 지난 투표를 닫고 vote.result를 보낸다. 닫은 개수를 돌려준다."""
    now = time.time() if now is None else now
    return await redis.eval(CLOSE, 3, vote_key(station_id, 'votes'), vote_key(station_id, 'vote-counts'),
                            f'engine:{station_id}:in', repr(float(now)), station_id)


def public_vote(vote, now, mine=None):
    """화면에 보낼 투표. 표 수는 창이 닫힌 뒤에만 싣는다 — 열린 동안의 집계가 투표를 흔들지 않게."""
    shown = {key: vote[key] for key in ('vote_id', 'kind', 'question', 'options', 'status')}
    shown |= dict(opens_at=float(vote['opens_at']), closes_at=float(vote['closes_at']),
                  remaining_sec=max(0.0, round(float(vote['closes_at']) - now, 1)))
    if vote['status'] == 'closed':
        shown |= dict(counts=vote.get('counts') or {}, total=vote.get('total', 0))
    if mine is not None:
        shown['mine'] = mine or None
    return shown


async def current_vote(redis, station_id, now=None):
    """지금 열린 투표, 또는 닫힌 지 RESULT_SHOWN_SECONDS 안 된 마지막 투표. 없으면 None."""
    now = time.time() if now is None else now
    vote_id = await redis.get(vote_key(station_id, 'current-vote'))
    raw = await redis.hget(vote_key(station_id, 'votes'), vote_id) if vote_id else None
    if not raw:
        return None
    vote = json.loads(raw)
    if vote['status'] == 'closed' and now - float(vote['closes_at']) > RESULT_SHOWN_SECONDS:
        return None
    return vote


class BallotInput(BaseModel):
    option: str = Field(min_length=1, max_length=100)


@router.get('/{station_id}/votes/current')
async def get_current_vote(station_id: str, request: Request, user: User):
    """청취 화면의 투표 카드. mine은 내가 고른 선택지(안 했으면 null)."""
    station = station_runtime(request, station_id)
    now = time.time()
    try:
        async with asyncio.timeout(3):
            await close_due(station.redis, station_id, now)
            vote = await current_vote(station.redis, station_id, now)
            mine = await station.redis.hget(vote_key(station_id, 'vote-ballots'),
                                            f"{vote['vote_id']}:{user['id']}") if vote else None
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    return {'vote': public_vote(vote, now, mine or '') if vote else None}


@router.post('/{station_id}/votes/{vote_id}')
async def cast_vote(station_id: str, vote_id: str, body: BallotInput, request: Request, user: User):
    """한 표 던지기. 이미 투표했으면 처음 고른 선택지를 그대로 돌려주고, 다른 선택지면 409."""
    station = station_runtime(request, station_id)
    now = time.time()
    try:
        async with asyncio.timeout(3):
            await close_due(station.redis, station_id, now)
            result, choice = await station.redis.eval(
                BALLOT, 3, vote_key(station_id, 'votes'), vote_key(station_id, 'vote-ballots'),
                vote_key(station_id, 'vote-counts'), vote_id, user['id'], body.option, repr(now))
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    if result == 'missing':
        raise HTTPException(404, 'Vote not found')
    if result == 'closed':
        raise HTTPException(409, 'Vote is not open')
    if result == 'invalid':
        raise HTTPException(422, 'Option is not in this vote')
    if result == 'voted' and choice != body.option:
        raise HTTPException(409, {'message': 'Already voted', 'mine': choice})
    return {'vote_id': vote_id, 'mine': choice}
