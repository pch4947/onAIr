"""Durable listener requests and atomic engine delivery on the local Redis instance."""
from datetime import datetime, timezone
import hashlib
import json
import time
from uuid import uuid4


# Check key types before writing: Redis scripts isolate commands but do not roll back errors.
CREATE = """
local expected = {'hash', 'hash', 'stream', 'list'}
for i = 1, 4 do
  local kind = redis.call('TYPE', KEYS[i]).ok
  if kind ~= 'none' and kind ~= expected[i] then
    return redis.error_reply('Unexpected request key type')
  end
end
local previous = redis.call('HGET', KEYS[2], ARGV[1])
if previous then
  local entry = cjson.decode(previous)
  if entry.fingerprint ~= ARGV[2] then return {0, ''} end
  return {1, entry.id}
end
redis.call('XADD', KEYS[3], '*', 'type', 'request.arrived', 'contract_version', '1',
  'event_id', ARGV[5], 'station_id', ARGV[6], 'at', ARGV[7], 'payload', ARGV[8])
redis.call('HSET', KEYS[1], ARGV[3], ARGV[4])
redis.call('HSET', KEYS[2], ARGV[1], cjson.encode({id=ARGV[3], fingerprint=ARGV[2]}))
redis.call('RPUSH', KEYS[4], cjson.encode({state='requested', at=tonumber(ARGV[7]), source='api', accepted=true}))
return {1, ARGV[3]}
"""


class IdempotencyConflict(ValueError):
    pass


class RequestStore:
    def __init__(self, redis, station_id):
        self.redis = redis
        self.station_id = station_id
        self.prefix = f'backend:{station_id}'

    async def create(self, prompt, listener_id, kind, idempotency_key=None):
        request_id = str(uuid4())
        entry = dict(id=request_id, prompt=prompt, listenerId=listener_id, kind=kind,
                     status='requested', createdAt=datetime.now(timezone.utc).isoformat(timespec='milliseconds'))
        fingerprint = hashlib.sha256(json.dumps([prompt, listener_id, kind], ensure_ascii=False).encode()).hexdigest()
        # Separate automatic keys from client-provided keys.
        key = 'client:' + hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else 'auto:' + request_id
        accepted, stored_id = await self.redis.eval(CREATE, 4, self.prefix + ':requests',
            self.prefix + ':request-keys', f'engine:{self.station_id}:in',
            self.prefix + ':request-history:' + request_id,
            key, fingerprint, request_id, json.dumps(entry, ensure_ascii=False),
            'evt_' + uuid4().hex, self.station_id, str(time.time()),
            json.dumps(dict(request_id=request_id, kind=kind, body=prompt,
                            requester_ref=listener_id), ensure_ascii=False))
        if not accepted:
            raise IdempotencyConflict()
        return await self.get(stored_id)

    @staticmethod
    def merge(raw, state):
        entry = json.loads(raw)
        if state:
            entry['status'] = json.loads(state)['state']
        return entry

    async def get(self, request_id):
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hget(self.prefix + ':requests', request_id)
            pipe.hget(self.prefix + ':request-states', request_id)
            raw, state = await pipe.execute()
        return self.merge(raw, state) if raw else None

    async def list(self):
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hgetall(self.prefix + ':requests')
            pipe.hgetall(self.prefix + ':request-states')
            requests, states = await pipe.execute()
        return sorted((self.merge(raw, states.get(key)) for key, raw in requests.items()),
                      key=lambda entry: (entry['createdAt'], entry['id']))

    async def metrics(self):
        pending = [entry for entry in await self.list() if entry['status'] not in ('rejected', 'played', 'failed')]
        age = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(pending[0]['createdAt'])).total_seconds())) if pending else 0
        return dict(pendingRequestCount=len(pending), oldestRequestAgeSeconds=age,
                    requestStatsAvailable=True)
