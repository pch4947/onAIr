"""Local durable publication checkpoints and Redis event outbox (one publisher per station)."""
import json
import sqlite3
import time
import uuid


DELIVER = """
local expected = {'hash','stream','hash','list','hash','list'}
for i=1,6 do
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
    def __init__(self, path, station):
        self.station = station
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE,
            body TEXT NOT NULL, request_id TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS measurements(id INTEGER PRIMARY KEY, segment_id TEXT, stage TEXT,
            at REAL, duration_ms REAL);
        ''')

    def close(self):
        self.db.close()

    def status(self, segment_id):
        row = self.db.execute('SELECT status FROM jobs WHERE id=?', (segment_id,)).fetchone()
        return row[0] if row else None

    def transition(self, segment, state, at=None):
        at = time.time() if at is None else at
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO jobs VALUES(?,?,?)',
                            (segment['id'], state, json.dumps(segment)))
            request_id = segment.get('request_ref') if segment.get('kind') == 'speech' and segment.get('corner_type') == 'request_reply' else None
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
            segment_id = json.loads(body)['payload'].get('segment_id', '')
            await redis.eval(DELIVER, 6, prefix + ':broadcast-events', f'engine:{self.station}:in',
                prefix + ':segment-states', prefix + ':segment-history:' + segment_id,
                prefix + ':request-states', prefix + ':request-history:' + request_id,
                event_id, body, request_id)
            with self.db:
                self.db.execute('DELETE FROM outbox WHERE seq=?', (seq,))

    def pending(self):
        return self.db.execute('SELECT count(*) FROM outbox').fetchone()[0]
