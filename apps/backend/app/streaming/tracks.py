"""Atomic track announcements and durable server-clock start delivery."""
import json

# A song request shares its ID with its music segment; only that audio settles its state.
MUSIC_SEGMENT_PREFIX = 'music_'

REGISTER = """
local meta = cjson.decode(ARGV[5])
redis.call('HSET', KEYS[5], ARGV[1], ARGV[5])
if meta.source == 'playlist' then
 if redis.call('HEXISTS', KEYS[8], meta.track.track_ref) == 0 then redis.call('RPUSH', KEYS[9], meta.track.track_ref) end
 redis.call('HSET', KEYS[8], meta.track.track_ref, cjson.encode({segment=cjson.decode(ARGV[2]),meta=meta}))
end
local nextid = redis.call('HGET', KEYS[6], 'next')
-- 첫 곡(first)은 엔진이 station.created로 이미 알고 소개 없이 틀므로 track.queued를 보내지 않는다
-- DJ 선곡(dj)은 끼어들지 않는다 — 다음 곡이 비어 있을 때만 다음 곡이 된다 (신청곡 > 플레이리스트·DJ 선곡, 3.4절)
if meta.source ~= 'first' and (not nextid or (meta.source ~= 'playlist' and meta.source ~= 'dj' and redis.call('HEXISTS', KEYS[6], 'frozen') == 0)) then
 redis.call('HSET', KEYS[6], 'next', ARGV[1])
 redis.call('XADD', KEYS[7], '*', 'type', 'track.queued', 'contract_version', '1',
  'event_id', 'evt_track_' .. ARGV[1], 'station_id', ARGV[6], 'at', ARGV[7], 'payload', ARGV[5])
end
"""

START = """
local kinds = {'hash','stream','hash','hash','hash','list','hash','hash','list'}
for i=1,9 do
 local t = redis.call('TYPE', KEYS[i]).ok
 if t ~= 'none' and t ~= kinds[i] then return redis.error_reply('Unexpected track key type') end
end
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then return 0 end
local event = cjson.decode(ARGV[2])
redis.call('XADD', KEYS[2], '*', 'type', 'track.started', 'contract_version', '1',
 'event_id', ARGV[1], 'station_id', event.station_id, 'at', tostring(event.at), 'payload', cjson.encode(event.payload))
local current = event.segment_id
if redis.call('HGET', KEYS[4], 'next') == current then redis.call('HDEL', KEYS[4], 'next') end
if redis.call('HGET', KEYS[4], 'frozen') == current then redis.call('HDEL', KEYS[4], 'frozen') end
redis.call('HSET', KEYS[4], 'playing', current)
-- 청취자가 듣기 시작한 서버 시각. playing은 다음 곡이 시작될 때까지 남으므로 곡이 끝났는지는 이것과 길이로 안다
redis.call('HSET', KEYS[4], 'playing_at', tostring(event.at))
local chosen = nil
local urgent = nil
for _, id in ipairs(redis.call('LRANGE', KEYS[6], 0, -1)) do
 local raw = redis.call('HGET', KEYS[3], id)
 local state = redis.call('HGET', KEYS[7], id)
 local terminal = state and cjson.decode(state).state
 if id ~= current and raw and terminal ~= 'PUBLISHED' and terminal ~= 'PLAYED' and terminal ~= 'FAILED' then
  if not chosen then chosen = id end
  local source = cjson.decode(raw).source
  if source ~= 'playlist' and source ~= 'dj' then urgent = id end
 end
end
chosen = urgent or chosen
if not chosen and ARGV[3] == '1' then
 local refs = redis.call('LRANGE', KEYS[9], 0, -1)
 local index = 1
 for i, ref in ipairs(refs) do
  if ref == event.payload.track_ref then index = (i % #refs) + 1 end
 end
 if #refs > 0 then
  local saved = cjson.decode(redis.call('HGET', KEYS[8], refs[index]))
  chosen = 'music_cycle_' .. ARGV[1]
  saved.segment.id = chosen
  saved.segment.created_at = event.at
  redis.call('HSET', KEYS[5], chosen, cjson.encode(saved.segment))
  redis.call('RPUSH', KEYS[6], chosen)
  redis.call('HSET', KEYS[3], chosen, cjson.encode(saved.meta))
 end
end
if chosen then
 redis.call('HSET', KEYS[4], 'next', chosen)
 redis.call('XADD', KEYS[2], '*', 'type', 'track.queued', 'contract_version', '1',
  'event_id', ARGV[1] .. '_next', 'station_id', event.station_id, 'at', tostring(event.at),
  'payload', redis.call('HGET', KEYS[3], chosen))
end
redis.call('HSET', KEYS[1], ARGV[1], '1')
return 1
"""


async def now_playing(redis, prefix, now):
    """청취자가 지금 듣고 있는 곡의 대기열 항목(+ started_at). 곡이 끝났거나 모르면 None."""
    playing, started_at = await redis.hmget(prefix + ':track-plan', ['playing', 'playing_at'])
    if not playing or not started_at:
        return None
    raw = await redis.hget(prefix + ':music-submissions', playing)
    item = json.loads(raw) if raw else None
    duration_ms = ((item or {}).get('track') or {}).get('duration_ms')
    if not duration_ms or now >= float(started_at) + duration_ms / 1000:
        return None
    return {**item, 'started_at': float(started_at)}


async def deliver_start(redis, station, event_id, body, repeat_playlist):
    prefix = 'backend:' + station
    return await redis.eval(START, 9, prefix + ':broadcast-events', f'engine:{station}:in',
        prefix + ':tracks', prefix + ':track-plan', prefix + ':segments', prefix + ':queue',
        prefix + ':segment-states', prefix + ':playlist', prefix + ':playlist-order',
        event_id, body, '1' if repeat_playlist else '0')
