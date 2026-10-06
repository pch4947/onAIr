"""One station consumer; Redis-backed FIFO and dedup survive process restarts."""

import asyncio
import contextlib
import json
import logging
import socket
import uuid

from redis.exceptions import RedisError, ResponseError

from .contracts import Segment, decode, validate_audio

log = logging.getLogger(__name__)
GROUP = "backend"

# Dedicated keys owned by this module, on the single Redis instance used by this MVP.
ACCEPT = """
local old = redis.call('HGET', KEYS[1], ARGV[1])
if old then
  if old == ARGV[2] then return 0 else return -1 end
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
redis.call('RPUSH', KEYS[2], ARGV[1])
return 1
"""
QUARANTINE = """
if redis.call('HSETNX', KEYS[1], ARGV[1], '1') == 1 then
  redis.call('XADD', KEYS[2], '*', 'source_id', ARGV[1], 'reason', ARGV[2], 'data', ARGV[3])
end
"""
STATE = """
local old = redis.call('HGET', KEYS[1], ARGV[1])
if old then
  local p = string.find(old, '-')
  local q = string.find(ARGV[2], '-')
  local a, b = tonumber(string.sub(old,1,p-1)), tonumber(string.sub(ARGV[2],1,q-1))
  if a > b or (a == b and tonumber(string.sub(old,p+1)) >= tonumber(string.sub(ARGV[2],q+1))) then return 0 end
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
local previous = redis.call('HGET', KEYS[2], ARGV[1])
local oldstate = previous and cjson.decode(previous).state or 'requested'
local newstate = cjson.decode(ARGV[3]).state
local allowed = {
 requested={screened=1,queued=1,generating=1,generated=1,rejected=1},
 screened={queued=1,generating=1,generated=1,rejected=1},
 queued={generating=1,generated=1,rejected=1},
 generating={queued=1,generated=1,rejected=1},
 generated={rejected=1}
}
if oldstate == newstate then return 0 end
if not allowed[oldstate] or not allowed[oldstate][newstate] then
 redis.call('RPUSH', KEYS[3], cjson.encode({state=newstate, previous=oldstate,
   at=ARGV[4], event_id=ARGV[5], source='engine', accepted=false, reason='invalid_transition'}))
 return 0
end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[3])
redis.call('RPUSH', KEYS[3], cjson.encode({state=newstate, previous=oldstate,
   at=ARGV[4], event_id=ARGV[5], source='engine', accepted=true}))
return 1
"""


class EngineConsumer:
    def __init__(self, redis, settings):
        self.redis, self.settings = redis, settings
        self.prefix = f"backend:{settings.station_id}"
        self.stream = f"engine:{settings.station_id}:out"
        self.name = f"backend-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

    async def ensure_group(self):
        try:
            await self.redis.xgroup_create(self.stream, GROUP, id="0-0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def quarantine(self, entry_id, fields, reason):
        await self.redis.eval(QUARANTINE, 2, self.prefix + ":dead-seen", self.stream + ":dead",
                              entry_id, reason, json.dumps(fields, ensure_ascii=False))
        await self.ack(entry_id)

    async def ack(self, entry_id):
        await self.redis.xack(self.stream, GROUP, entry_id)
        await self.redis.hdel(self.prefix + ":attempts", entry_id)

    async def handle(self, entry_id, fields):
        try:
            event = decode(fields, self.settings.station_id)
        except (ValueError, TypeError):
            await self.quarantine(entry_id, fields, "invalid_contract")
            return
        if isinstance(event, Segment):
            encoded = json.dumps(event.model_dump(), sort_keys=True, ensure_ascii=False)
            old = await self.redis.hget(self.prefix + ":segments", event.id)
            if old is not None:
                if old != encoded:
                    await self.quarantine(entry_id, fields, "segment_id_conflict")
                else:
                    await self.ack(entry_id)
                return
            try:
                await asyncio.to_thread(validate_audio, self.settings.audio_dir, event)
            except OSError:
                attempts = await self.redis.hincrby(self.prefix + ":attempts", entry_id, 1)
                if attempts >= 3:
                    await self.quarantine(entry_id, fields, "audio_unavailable")
                return  # Keep pending for retry via XAUTOCLAIM.
            except ValueError:
                await self.quarantine(entry_id, fields, "invalid_audio_path_or_file")
                return
            result = await self.redis.eval(ACCEPT, 2, self.prefix + ":segments",
                                           self.prefix + ":queue", event.id, encoded)
            if result == -1:
                await self.quarantine(entry_id, fields, "segment_id_conflict")
                return
        else:
            # Source stream ID prevents older pending events overwriting newer states.
            await self.redis.eval(STATE, 3, self.prefix + ":request-versions",
                                  self.prefix + ":request-states", self.prefix + ":request-history:" + event.request_id,
                                  event.request_id, entry_id, json.dumps(event.model_dump()), fields['at'], fields['event_id'])
        await self.ack(entry_id)

    async def run(self):
        cursor = "0-0"
        while True:
            try:
                await self.ensure_group()
                reclaimed = await self.redis.xautoclaim(
                    self.stream, GROUP, self.name, min_idle_time=30000, start_id=cursor, count=20)
                cursor = reclaimed[0]
                for entry_id, fields in reclaimed[1]:
                    await self.handle(entry_id, fields)
                batches = await self.redis.xreadgroup(
                    GROUP, self.name, {self.stream: ">"}, count=20, block=1000)
                for _, entries in batches:
                    for entry_id, fields in entries:
                        await self.handle(entry_id, fields)
            except (RedisError, OSError):
                log.warning("Redis consumer unavailable; retrying in 3 seconds")
                await asyncio.sleep(3)
            except Exception:
                log.exception("Consumer processing failed; pending messages retained")
                await asyncio.sleep(3)


async def stop_consumer(task):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
