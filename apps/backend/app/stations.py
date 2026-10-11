"""Authenticated creation of the configured single station and engine REST bridge."""
import asyncio
import hashlib
import json
import time
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from onair_schema import Persona, PersonaForm, StationCreated
from onair_schema.persona import text
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError

from .auth import get_current_user

router = APIRouter(prefix='/api')
User = Annotated[dict, Depends(get_current_user)]

CREATE = """
local kinds = {'string', 'hash', 'stream'}
for i=1,3 do
 local t = redis.call('TYPE', KEYS[i]).ok
 if t ~= 'none' and t ~= kinds[i] then return redis.error_reply('Unexpected station key type') end
end
local old = redis.call('GET', KEYS[1])
if old then
 local s = cjson.decode(old)
 if s.owner_id == ARGV[1] and s.retry_key == ARGV[2] and s.fingerprint == ARGV[3] then return old end
 return ''
end
redis.call('HSET', KEYS[2], ARGV[4], ARGV[5])
redis.call('SET', KEYS[1], ARGV[6])
redis.call('XADD', KEYS[3], '*', 'type', 'station.created', 'contract_version', '1',
 'event_id', ARGV[7], 'station_id', ARGV[8], 'at', ARGV[9], 'payload', ARGV[10])
return ARGV[6]
"""

RESULT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local s = cjson.decode(raw)
if s.status ~= 'preparing' or tonumber(ARGV[1]) < s.created_at then return 0 end
s.status = ARGV[2]
s.engine_result = cjson.decode(ARGV[3])
s.updated_at = tonumber(ARGV[1])
redis.call('SET', KEYS[1], cjson.encode(s))
return 1
"""


class StationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    broadcast_minutes: int = Field(ge=30, le=60)
    persona: Persona
    topic: text(60) | None = None
    policy: Literal['naive_fifo'] = 'naive_fifo'
    # Music playback is not implemented yet; do not silently accept a first_song.


class DraftInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    form: PersonaForm
    count: int = Field(default=3, ge=1, le=5)


class CheckInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    persona: Persona


class StationStore:
    def __init__(self, redis, station_id):
        self.redis, self.station_id = redis, station_id
        self.key = f'backend:{station_id}:station'

    async def create(self, body, owner, retry_key):
        fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        persona = body.persona.model_copy(update={'persona_id': 'psn_' + uuid4().hex})
        payload = StationCreated(**{**body.model_dump(), 'persona': persona}).model_dump()
        now = time.time()
        record = dict(station_id=self.station_id, owner_id=owner, status='preparing',
                      created_at=now, updated_at=now, config=payload,
                      retry_key=retry_key, fingerprint=fingerprint, engine_result=None)
        raw = await self.redis.eval(CREATE, 3, self.key, f'backend:{self.station_id}:personas',
            'engine:control', owner, retry_key, fingerprint, persona.persona_id,
            persona.model_dump_json(), json.dumps(record), 'evt_' + uuid4().hex,
            self.station_id, str(now), json.dumps(payload))
        if not raw:
            raise HTTPException(409, 'Configured station already exists; replay the same request and Idempotency-Key')
        return json.loads(raw)

    async def result(self, kind, payload, at):
        await self.redis.eval(RESULT, 1, self.key, str(at),
                              'started' if kind == 'station.started' else 'rejected', json.dumps(payload))


def public_station(record, request):
    # Engine started does not imply HLS ready, and fallback readiness does not imply engine started.
    hls = request.app.state.hls
    return {k: v for k, v in record.items() if k not in ('retry_key', 'fingerprint', 'owner_id')} | {
        'hls_ready': bool(request.app.state.settings.hls_enabled and hls.ready),
        'hls_url': f'/hls/{record["station_id"]}/index.m3u8'}


@router.post('/stations', status_code=202)
async def create_station(body: StationInput, request: Request, user: User,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9._:-]+$')]):
    """Create the configured station once. 202 means queued, not live. Retry with the same key."""
    if body.persona.persona_id is not None:
        raise HTTPException(422, 'Submit a persona draft without persona_id; the backend assigns it')
    try:
        async with asyncio.timeout(3):
            record = await request.app.state.stations.create(body, user['id'], idempotency_key)
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable; retry with the same Idempotency-Key') from None
    return {'station': public_station(record, request)}


@router.get('/stations/{station_id}')
async def get_station(station_id: str, request: Request, response: Response, user: User):
    response.headers['Cache-Control'] = 'no-store'
    if station_id != request.app.state.settings.station_id:
        raise HTTPException(404, 'Station not found')
    try:
        async with asyncio.timeout(3):
            raw = await request.app.state.redis.get(request.app.state.stations.key)
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    if not raw or json.loads(raw)['owner_id'] != user['id']:
        raise HTTPException(404, 'Station not found')
    return {'station': public_station(json.loads(raw), request)}


async def engine_call(request, method, path, body=None, params=None):
    try:
        async with asyncio.timeout(30):
            response = await request.app.state.engine_http.request(method, path, json=body, params=params)
    except (httpx.TimeoutException, TimeoutError):
        raise HTTPException(504, 'Engine response timed out') from None
    except httpx.RequestError:
        raise HTTPException(503, 'Engine unavailable') from None
    if response.status_code == 422:
        # Preserve field-level validation errors, but never forward upstream credentials/tracebacks.
        try:
            payload = response.json()
            if isinstance(payload, dict):
                for key in ('errors', 'detail'):
                    if isinstance(payload.get(key), list):
                        return JSONResponse(status_code=422, content={key: payload[key]})
        except ValueError:
            pass
        raise HTTPException(502, 'Invalid engine validation response')
    if response.status_code == 503:
        raise HTTPException(503, 'Engine unavailable; try again shortly')
    if not response.is_success:
        raise HTTPException(502, 'Engine request failed')
    try:
        return response.json()
    except ValueError:
        raise HTTPException(502, 'Invalid engine response') from None


@router.get('/voices')
async def voices(request: Request, user: User, native_only: bool = True):
    return await engine_call(request, 'GET', '/v1/voices', params={'native_only': str(native_only).lower()})


@router.post('/personas/drafts')
async def drafts(body: DraftInput, request: Request, user: User):
    return await engine_call(request, 'POST', '/v1/personas/drafts', body.model_dump())


@router.post('/personas/check')
async def check(body: CheckInput, request: Request, user: User):
    return await engine_call(request, 'POST', '/v1/personas/check', body.model_dump())
