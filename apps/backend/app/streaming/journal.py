"""Local durable publication checkpoints and Redis event outbox (one publisher per station)."""
import json
import sqlite3
import time
import uuid
from .tracks import deliver_start


DELIVER = """
local expected = {'hash','stream','hash','list','hash','list','hash','hash'}
for i=1,8 do
 local kind = redis.call('TYPE', KEYS[i]).ok
 if kind ~= 'none' and kind ~= expected[i] then return redis.error_reply('Unexpected broadcast key type') end
end
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then return 0 end
local event = cjson.decode(ARGV[2])
if event.type == 'state.transition' then
  local p = event.payload
  redis.call('HSET', KEYS[3], p.segment_id, cjson.encode(p))
  redis.call('RPUSH', KEYS[4], cjson.encode(p))
  if ARGV[3] ~= '' then
    local old = redis.call('HGET', KEYS[5], ARGV[3])
    local oldstate = old and cjson.decode(old).state or 'generated'
    if oldstate ~= 'rejected' and oldstate ~= 'failed' and oldstate ~= 'played' then
      local state = string.lower(p.state)
      local updated = cjson.encode({request_id=ARGV[3], state=state})
      redis.call('HSET', KEYS[5], ARGV[3], updated)
      redis.call('RPUSH', KEYS[6], cjson.encode({state=state, at=p.at, accepted=true,
        source='broadcast', segment_id=p.segment_id, event_id=ARGV[1]}))
    end
  end
elseif event.type == 'encoding.failed' then
  local failed = event.payload.segment_id
  local reserved = redis.call('HGET', KEYS[8], failed) or failed
  if redis.call('HGET', KEYS[7], 'frozen') == reserved then redis.call('HDEL', KEYS[7], 'frozen') end
  if redis.call('HGET', KEYS[7], 'next') == failed then redis.call('HDEL', KEYS[7], 'next') end
  redis.call('HSET', KEYS[3], event.payload.segment_id, cjson.encode(event.payload))
  if ARGV[3] ~= '' then
    local old = redis.call('HGET', KEYS[5], ARGV[3])
    local state = old and cjson.decode(old).state or ''
    if state ~= 'played' and state ~= 'rejected' then
      redis.call('HSET', KEYS[5], ARGV[3], cjson.encode({request_id=ARGV[3], state='failed'}))
      redis.call('RPUSH', KEYS[6], cjson.encode({state='failed', at=event.at, source='broadcast', accepted=true}))
    end
  end
end
if event.type ~= 'encoding.failed' then
  redis.call('XADD', KEYS[2], '*', 'type', event.type, 'contract_version', '1',
    'event_id', ARGV[1], 'station_id', event.station_id, 'at', tostring(event.at),
    'payload', cjson.encode(event.payload))
end
redis.call('HSET', KEYS[1], ARGV[1], '1')
return 1
"""


class Journal:
    def __init__(self, path, station, repeat_playlist=False):
        self.station = station
        self.repeat_playlist = repeat_playlist
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE,
            body TEXT NOT NULL, request_id TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS measurements(id INTEGER PRIMARY KEY, segment_id TEXT, stage TEXT,
            at REAL, duration_ms REAL);
          CREATE TABLE IF NOT EXISTS track_starts(segment_id TEXT PRIMARY KEY, started_at REAL);
        ''')

    def close(self):
        self.db.close()

    def status(self, segment_id):
        row = self.db.execute('SELECT status FROM jobs WHERE id=?', (segment_id,)).fetchone()
        return row[0] if row else None

    def transition(self, segment, state, at=None):
        at = time.time() if at is None else at
        with self.db:
            # Preserve first-claim order when a prefetched job exists before the
            # current source transitions to PUBLISHED (REPLACE changes rowid).
            self.db.execute('INSERT INTO jobs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET '
                            'status=excluded.status, payload=excluded.payload',
                            (segment['id'], state, json.dumps(segment)))
            # Quotes can belong to any speech corner, not just request_reply.
            # Music requests are completed by their own audio, not by an unrelated introduction.
            ref = segment.get('request_ref')
            request_id = ref if segment.get('kind') == 'speech' or (
                segment.get('kind') == 'music' and ref == segment.get('id')) else None
            self._enqueue('encoding.failed' if state == 'FAILED' else 'state.transition',
                          dict(segment_id=segment['id'], state=state, at=at), request_id or '', at)

    def _enqueue(self, kind, payload, request_id='', at=None):
        at = time.time() if at is None else at
        event = dict(type=kind, station_id=self.station, at=at, payload=payload)
        self.db.execute('INSERT INTO outbox(id,body,request_id) VALUES(?,?,?)',
                        ('evt_' + uuid.uuid4().hex, json.dumps(event), request_id))

    def enqueue(self, kind, payload):
        with self.db:
            self._enqueue(kind, payload)

    def track_started(self, segment, started_at):
        with self.db:
            inserted = self.db.execute('INSERT OR IGNORE INTO track_starts VALUES(?,?)',
                                      (segment['id'], started_at)).rowcount
            if inserted:
                event = dict(type='track.started', station_id=self.station, at=started_at,
                    segment_id=segment['id'], payload=dict(track_ref=segment['music_ref'], started_at=started_at))
                self.db.execute('INSERT INTO outbox(id,body,request_id) VALUES(?,?,?)',
                    ('evt_' + uuid.uuid4().hex, json.dumps(event), ''))

    def measure(self, segment_id, stage, duration_ms):
        with self.db:
            self.db.execute('INSERT INTO measurements(segment_id,stage,at,duration_ms) VALUES(?,?,?,?)',
                            (segment_id, stage, time.time(), duration_ms))

    def recover(self):
        # Resume nonterminal sources from their beginning on a new HLS session.
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM jobs WHERE status IN ('MIXING','PUBLISHED') ORDER BY rowid")]

    async def flush(self, redis, limit=20):
        prefix = f'backend:{self.station}'
        for seq, event_id, body, request_id in self.db.execute(
                'SELECT seq,id,body,request_id FROM outbox ORDER BY seq LIMIT ?', (limit,)).fetchall():
            event = json.loads(body)
            if event['type'] == 'track.started':
                if event['payload']['started_at'] > time.time():
                    continue
                await deliver_start(redis, self.station, event_id, body, self.repeat_playlist)
            else:
                segment_id = event['payload'].get('segment_id', '')
                await redis.eval(DELIVER, 8, prefix + ':broadcast-events', f'engine:{self.station}:in',
                    prefix + ':segment-states', prefix + ':segment-history:' + segment_id,
                    prefix + ':request-states', prefix + ':request-history:' + request_id,
                      prefix + ':track-plan', prefix + ':intro-tracks',
                    event_id, body, request_id)
            with self.db:
                self.db.execute('DELETE FROM outbox WHERE seq=?', (seq,))

    def pending(self):
        return self.db.execute('SELECT count(*) FROM outbox').fetchone()[0]
