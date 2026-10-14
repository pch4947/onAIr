"""Single-station end detection and restart-safe cleanup; never infer death from a timeout."""
import asyncio
import json
import logging
import time
from uuid import uuid4

from fastapi import HTTPException

from .streaming.hls import HlsPublisher

log = logging.getLogger(__name__)
UPDATE = """
local raw = redis.call('GET', KEYS[1])
if raw ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2])
return 1
"""
CLEAN = """
local raw = redis.call('GET', KEYS[1])
if raw ~= ARGV[1] then return 0 end
local prefix = ARGV[4]
for _, key in ipairs({prefix .. ':requests', prefix .. ':request-states', prefix .. ':replay-sessions'}) do
 local kind = redis.call('TYPE', key).ok
 if kind ~= 'none' and kind ~= 'hash' then return redis.error_reply('Unexpected request key type') end
end
local requests = redis.call('HGETALL', prefix .. ':requests')
for i=1,#requests,2 do
 local kind = redis.call('TYPE', prefix .. ':request-history:' .. requests[i]).ok
 if kind ~= 'none' and kind ~= 'list' then return redis.error_reply('Unexpected request history type') end
 local previous = redis.call('HGET', prefix .. ':request-states', requests[i])
 if previous then cjson.decode(previous) end
end
local replays = redis.call('HGETALL', prefix .. ':replay-sessions')
for i=1,#replays,2 do cjson.decode(replays[i+1]) end
for i=2,#KEYS do
 if redis.call('EXISTS', KEYS[i]) == 1 then
  redis.call('RENAME', KEYS[i], ARGV[3] .. KEYS[i])
 end
end
for i=1,#requests,2 do
 local id = requests[i]
 local previous = redis.call('HGET', ARGV[4] .. ':request-states', id)
 local state = previous and cjson.decode(previous).state or 'requested'
 if state ~= 'played' and state ~= 'failed' and state ~= 'rejected' then
  redis.call('HSET', ARGV[4] .. ':request-states', id,
   cjson.encode({request_id=id, state='failed'}))
  redis.call('RPUSH', ARGV[4] .. ':request-history:' .. id,
   cjson.encode({state='failed', previous=state, source='backend', reason='broadcast_ended',
    accepted=true, at=tonumber(ARGV[5])}))
 end
end
local station = cjson.decode(raw)
redis.call('SET', ARGV[3] .. KEYS[1], raw)
for i=1,#replays,2 do
 local replay = cjson.decode(replays[i+1])
 if not replay.ownerId then
  replay.ownerId = station.owner_id
  redis.call('HSET', prefix .. ':replay-sessions', replays[i], cjson.encode(replay))
 end
end
redis.call('SET', KEYS[1], ARGV[2])
return 1
"""


class StationLifecycle:
    def __init__(self, app):
        self.app = app
        self.state = app.state
        self.redis = app.state.redis
        self.sid = app.state.settings.station_id
        self.key = f'backend:{self.sid}:station'
        self.lock = asyncio.Lock()
        self.absences = 0
        self.observed = None

    async def update(self, raw, record, status):
        record = dict(record, status=status, updated_at=time.time())
        encoded = json.dumps(record)
        if not await self.redis.eval(UPDATE, 1, self.key, raw, encoded):
            return None
        return encoded

    async def request_stop(self, owner, created_at):
        async with self.lock:
            raw = await self.redis.get(self.key)
            record = json.loads(raw) if raw else None
            if not record or record['owner_id'] != owner:
                raise HTTPException(404, 'Station not found')
            if record['created_at'] != created_at:
                raise HTTPException(409, 'Broadcast changed; reload before stopping')
            if record['status'] not in ('ended', 'cleaning', 'stopping'):
                updated = await self.update(raw, record, 'stopping')
                if updated is None:
                    raise HTTPException(409, 'Broadcast changed; retry')
                record = json.loads(updated)
            # The durable stopping state is retried by tick, even if this call disconnects.
            return record

    async def control_consumed(self, record):
        entry = record.get('control_id')
        if not entry:
            return record['status'] != 'preparing'  # Legacy records have no control ID.
        groups = await self.redis.xinfo_groups('engine:control')
        group = next((g for g in groups if g['name'] == 'engine'), None)
        number = lambda value: tuple(map(int, value.split('-')))
        if not group or number(group['last-delivered-id']) < number(entry):
            return False
        return not await self.redis.xpending_range('engine:control', 'engine', entry, entry, 1)

    async def output_drained(self):
        groups = await self.redis.xinfo_groups(f'engine:{self.sid}:out')
        group = next((g for g in groups if g['name'] == 'backend'), None)
        return bool(group and group.get('lag') == 0 and group['pending'] == 0)

    async def cleanup(self, raw, record):
        # cleaning is a durable recovery marker; creation remains blocked until the final SET.
        if record['status'] != 'cleaning':
            record = dict(record, cleanup_session=getattr(self.state.hls, 'session', None))
            raw = await self.update(raw, record, 'cleaning')
            if raw is None:
                return
            record = json.loads(raw)
        publisher = self.state.hls
        await publisher.stop()
        if getattr(publisher, 'vod_entries', None):
            publisher.write_vod_manifest(final=True)
        if record.get('cleanup_session') and self.state.replay_sessions:
            await self.state.replay_sessions.end(record['cleanup_session'], time.time())
        generation = record.get('generation') or str(record['created_at']).replace('.', '-')
        for suffix in ('', '-wal', '-shm'):
            source = publisher.directory / ('broadcast.sqlite' + suffix)
            if source.exists():
                destination = publisher.directory / f'ended-{generation}.sqlite{suffix}'
                if destination.exists():
                    raise RuntimeError('Archived journal already exists; refusing to overwrite')
                source.rename(destination)
        prefix = f'backend:{self.sid}'
        # Keep segment/request histories, personas, original audio and VOD. Archive live lists.
        keys = [prefix + ':' + name for name in (
            'queue', 'playlist', 'playlist-order', 'track-plan', 'tracks', 'intro-tracks',
            'music-submissions')]
        # The stopped engine's pending station.closed must never stop the next instance.
        # Engine absence and drained output are checked before rotating consumer groups.
        keys += [f'engine:{self.sid}:in', f'engine:{self.sid}:out']
        ended = dict(record, status='ended', updated_at=time.time(), ended_at=time.time())
        if await self.redis.eval(CLEAN, len(keys) + 1, self.key, *keys, raw,
                                 json.dumps(ended), f'archive:{generation}:', prefix, str(time.time())):
            self.state.hls = HlsPublisher(self.redis, self.state.settings, self.state.replay_sessions)

    async def tick(self):
        async with self.lock:
            raw = await self.redis.get(self.key)
            if not raw:
                return
            record = json.loads(raw)
            status = record['status']
            if status == 'ended':
                return
            if status == 'cleaning':
                await self.cleanup(raw, record)
                return
            if status == 'stopping' and self.state.hls.started:
                await self.state.hls.stop()
            elif (status != 'stopping' and self.state.settings.hls_enabled
                  and not self.state.hls.started):
                await self.state.hls.start()
            response = await self.state.engine_http.get('/v1/engine/status', timeout=3)
            response.raise_for_status()
            payload = response.json()
            stations = payload.get('stations')
            if not isinstance(stations, list) or any(
                    not isinstance(s, dict) or not isinstance(s.get('station_id'), str) for s in stations):
                raise ValueError('Invalid engine station list')
            present = any(s['station_id'] == self.sid for s in stations)
            identity = record['created_at']
            if identity != self.observed:
                self.absences = 0
                self.observed = identity
            if status == 'stopping' and present:
                await self.redis.xadd(f'engine:{self.sid}:in', {
                    'type': 'station.closed', 'contract_version': '1', 'event_id': 'evt_' + uuid4().hex,
                    'station_id': self.sid, 'at': str(time.time()), 'payload': json.dumps({'reason': 'operator_stop'})})
            if present or not await self.control_consumed(record):
                self.absences = 0
                return
            self.absences += 1
            if self.absences < 3:
                return
            if status not in ('draining', 'stopping', 'rejected'):
                raw = await self.update(raw, record, 'draining')
                if raw is None:
                    return
                record = json.loads(raw)
            publisher = self.state.hls
            if publisher.journal:
                publisher.journal.repeat_playlist = False
            if record['status'] == 'draining' and self.state.settings.hls_enabled:
                snapshot = publisher.buffer_snapshot()
                if (snapshot['totalAudioEstimateSeconds'] is None or snapshot['totalAudioEstimateSeconds'] > 0
                        or publisher.active_id or publisher.recovery or publisher.finishing
                        or snapshot['pendingStateEvents']):
                    return
            if not await self.output_drained():
                return
            await self.cleanup(raw, record)

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                self.absences = 0
                log.exception('Station cleanup deferred; state retained for retry')
            await asyncio.sleep(5)
