"""Creation of the configured single station by non-guest users and engine REST bridge."""
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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from redis.exceptions import RedisError

from .auth import get_broadcast_host, get_current_user
from .station_runtime import default_runtime, station_runtime

router = APIRouter(prefix='/api')
User = Annotated[dict, Depends(get_current_user)]
Host = Annotated[dict, Depends(get_broadcast_host)]

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
 if s.status ~= 'ended' then return '' end
end
redis.call('HSET', KEYS[2], ARGV[4], ARGV[5])
-- 첫 노래가 있으면 음원을 받은 뒤 RELEASE가 station.created를 보낸다
if ARGV[11] == '1' then
 redis.call('SET', KEYS[1], ARGV[6])
 return ARGV[6]
end
local control_id = redis.call('XADD', KEYS[3], '*', 'type', 'station.created', 'contract_version', '1',
 'event_id', ARGV[7], 'station_id', ARGV[8], 'at', ARGV[9], 'payload', ARGV[10])
-- Preserve empty JSON arrays in persona; Redis cjson would turn them into objects.
local encoded = string.sub(ARGV[6], 1, -2) .. ', "control_id":' .. cjson.encode(control_id) .. '}'
redis.call('SET', KEYS[1], encoded)
return encoded
"""

# 첫 노래 준비가 끝난 방의 station.created를 보낸다. 그 사이 기록이 바뀌었으면(종료 등) 보내지 않는다.
RELEASE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return '' end
local control_id = redis.call('XADD', KEYS[2], '*', 'type', 'station.created', 'contract_version', '1',
 'event_id', ARGV[3], 'station_id', ARGV[4], 'at', ARGV[5], 'payload', ARGV[6])
local encoded = string.sub(ARGV[2], 1, -2) .. ', "control_id":' .. cjson.encode(control_id) .. '}'
redis.call('SET', KEYS[1], encoded)
return encoded
"""

RESULT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local s = cjson.decode(raw)
if s.status ~= 'preparing' or tonumber(ARGV[1]) < s.created_at then return 0 end
if raw ~= ARGV[4] then return 0 end
redis.call('SET', KEYS[1], ARGV[5])
return 1
"""


class StationInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    broadcast_minutes: int = Field(ge=30, le=60)
    persona: Persona
    topic: text(60) | None = None
    policy: Literal['naive_fifo'] = 'naive_fifo'
    format: Literal['music', 'talk', 'news'] = Field(default='talk', description='M3 engine required for format selection')
    corners: list[Literal['story_corner', 'text_corner', 'topic_talk', 'dj_vs_listeners']] | None = Field(
        default=None, min_length=2, max_length=3, description='Talk format only; omitted uses engine defaults')
    news_sections: list[Literal['general', 'economy', 'it', 'sports', 'culture']] | None = Field(
        default=None, min_length=2, max_length=3, description='News format only; omitted uses engine defaults')
    intensity: Literal['low', 'mid', 'high'] = Field(default='mid', description='Ignored by the engine for news')
    first_song_url: str | None = Field(default=None, max_length=2048,
                                       description='YouTube link played without introduction at broadcast start')

    @field_validator('first_song_url')
    @classmethod
    def validate_first_song_url(cls, value):
        if value is None:
            return None
        from .music import youtube_id  # music imports this module
        youtube_id(value)
        return value

    @model_validator(mode='after')
    def validate_format_options(self):
        for name, expected in (('corners', 'talk'), ('news_sections', 'news')):
            values = getattr(self, name)
            if values is not None:
                if self.format != expected:
                    raise ValueError(f'{name} is only valid for {expected}')
                if len(set(values)) != len(values):
                    raise ValueError(f'{name} must not contain duplicates')
        return self

    def format_options(self):
        names = {'format', 'corners', 'news_sections', 'intensity'}
        if not self.model_fields_set & names:
            return {}
        return self.model_dump(include=names)

    def legacy_fields(self):
        return self.model_dump(exclude={'format', 'corners', 'news_sections', 'intensity', 'first_song_url'})


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
        # Preserve fingerprints and wire shape for clients that omit M3 fields.
        fields = body.legacy_fields() | body.format_options()
        if body.first_song_url:
            fields['first_song_url'] = body.first_song_url
        fingerprint = hashlib.sha256(json.dumps(fields, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        persona = body.persona.model_copy(update={'persona_id': 'psn_' + uuid4().hex})
        payload = StationCreated(**{**body.legacy_fields(), 'persona': persona}).model_dump(
            exclude={'format', 'corners', 'news_sections', 'intensity', 'experiment'}) | body.format_options()
        now = time.time()
        record = dict(station_id=self.station_id, owner_id=owner, status='preparing',
                      generation=uuid4().hex,
                      created_at=now, updated_at=now, config=payload,
                      retry_key=retry_key, fingerprint=fingerprint, engine_result=None)
        if body.first_song_url:
            record |= dict(first_song_url=body.first_song_url, first_song_status='preparing')
        raw = await self.redis.eval(CREATE, 3, self.key, f'backend:{self.station_id}:personas',
            'engine:control', owner, retry_key, fingerprint, persona.persona_id,
            persona.model_dump_json(), json.dumps(record), 'evt_' + uuid4().hex,
            self.station_id, str(now), json.dumps(payload), '1' if body.first_song_url else '0')
        if not raw:
            raise HTTPException(409, 'Configured station already exists; replay the same request and Idempotency-Key')
        return json.loads(raw)

    async def release(self, generation, first_song):
        """첫 노래 준비가 끝난(또는 실패한) 방의 station.created를 보낸다. 보냈으면 기록, 아니면 None."""
        for _ in range(5):
            raw = await self.redis.get(self.key)
            record = json.loads(raw) if raw else None
            if (not record or record.get('generation') != generation or record['status'] != 'preparing'
                    or record.get('control_id')):
                return None
            payload = dict(record['config'])
            if first_song:
                payload['first_song'] = first_song
            updated = dict(record, config=payload, first_song_status='ready' if first_song else 'failed',
                           updated_at=time.time())
            released = await self.redis.eval(RELEASE, 2, self.key, 'engine:control', raw, json.dumps(updated),
                'evt_' + uuid4().hex, self.station_id, str(time.time()), json.dumps(payload))
            if released:
                return json.loads(released)
        return None

    async def result(self, kind, payload, at):
        raw = await self.redis.get(self.key)
        if not raw:
            return
        record = json.loads(raw)
        status = 'started' if kind == 'station.started' else 'rejected'
        updated = dict(record, status=status, engine_result=payload, updated_at=at)
        # Keep persona arrays and the exact created_at used by the stop API.
        await self.redis.eval(RESULT, 1, self.key, str(at),
                              status, json.dumps(payload), raw, json.dumps(updated))


def public_station(record, request):
    # Engine started does not imply HLS ready, and fallback readiness does not imply engine started.
    hls = request.app.state.runtimes[record['station_id']].hls
    return {k: v for k, v in record.items() if k not in ('retry_key', 'fingerprint', 'owner_id', 'control_id')} | {
        'hls_ready': bool(request.app.state.settings.hls_enabled and hls.ready
                          and record['status'] not in ('stopping', 'cleaning', 'ended', 'rejected')),
        'hls_url': f'/hls/{record["station_id"]}/index.m3u8'}


@router.post('/stations', status_code=202)
async def create_station(body: StationInput, request: Request, user: Host,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9._:-]+$')]):
    """Create the configured station once. 202 means queued, not live. Retry with the same key."""
    if body.persona.persona_id is not None:
        raise HTTPException(422, 'Submit a persona draft without persona_id; the backend assigns it')
    # 채널 선택은 다중 방송 3단계. 지금은 기본 방송 하나에 만든다
    station = default_runtime(request.app)
    try:
        async with asyncio.timeout(3):
            if station.lifecycle:
                async with station.lifecycle.lock:
                    record = await station.stations.create(body, user['id'], idempotency_key)
            else:
                record = await station.stations.create(body, user['id'], idempotency_key)
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable; retry with the same Idempotency-Key') from None
    from .first_song import ensure_first_song_task  # first_song imports this module
    ensure_first_song_task(station, record)
    return {'station': public_station(record, request)}


@router.get('/stations/{station_id}')
async def get_station(station_id: str, request: Request, response: Response, user: User):
    response.headers['Cache-Control'] = 'no-store'
    station = station_runtime(request, station_id)
    try:
        async with asyncio.timeout(3):
            raw = await request.app.state.redis.get(station.stations.key)
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    if not raw or json.loads(raw)['owner_id'] != user['id']:
        raise HTTPException(404, 'Station not found')
    return {'station': public_station(json.loads(raw), request)}


class StopInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    created_at: float = Field(gt=0, allow_inf_nan=False)


@router.post('/stations/{station_id}/stop', status_code=202)
async def stop_station(station_id: str, body: StopInput, request: Request, user: User):
    """Owner-only, idempotent stop and cleanup. Keeps VOD; 202 means cleanup is pending.

    Send the created_at from the owner lookup to avoid stopping a replacement broadcast.
    """
    station = station_runtime(request, station_id)
    try:
        async with asyncio.timeout(5):
            record = await station.lifecycle.request_stop(user['id'], body.created_at)
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Cleanup unavailable; safely retry the same request') from None
    return {'station': public_station(record, request)}


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
async def drafts(body: DraftInput, request: Request, user: Host):
    return await engine_call(request, 'POST', '/v1/personas/drafts', body.model_dump())


@router.post('/personas/check')
async def check(body: CheckInput, request: Request, user: Host):
    return await engine_call(request, 'POST', '/v1/personas/check', body.model_dump())
