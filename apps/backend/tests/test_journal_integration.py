"""Real Redis lifecycle and retry after a committed event's response is lost."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from redis.asyncio import Redis
from redis.exceptions import ConnectionError

from app.messaging.consumer import STATE
from app.streaming.journal import Journal


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires local Redis')
class JournalIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_retry_dedup_and_late_engine_event(self):
        station = 'st_journal_' + uuid.uuid4().hex
        prefix = f'backend:{station}'
        client = Redis.from_url('redis://127.0.0.1:6379/0', decode_responses=True)
        try:
            async def engine_state(state, version):
                return await client.eval(STATE, 3, prefix + ':versions', prefix + ':request-states',
                    prefix + ':request-history:req_one', 'req_one', f'{version}-0',
                    json.dumps(dict(request_id='req_one', state=state)), '1.0', f'evt_{version}')
            for version, state in enumerate(['screened', 'queued', 'generating', 'queued', 'generating', 'generated'], 1):
                self.assertEqual(await engine_state(state, version), 1)
            with tempfile.TemporaryDirectory() as directory:
                journal = Journal(Path(directory) / 'broadcast.sqlite', station)
                try:
                    segment = dict(id='seg_one', kind='speech', corner_type='request_reply', request_ref='req_one')
                    journal.transition(segment, 'MIXING')
                    class LostReply:
                        async def eval(self, *args):
                            await client.eval(*args)
                            raise ConnectionError('reply lost after commit')
                    with self.assertRaises(ConnectionError):
                        await journal.flush(LostReply())
                    self.assertEqual(journal.pending(), 1)
                    await journal.flush(client)
                    self.assertEqual(await client.xlen(f'engine:{station}:in'), 1)
                    self.assertEqual(await engine_state('rejected', 7), 0)
                    for state in ['PUBLISHED', 'PLAYED']:
                        journal.transition(segment, state)
                    journal.transition(dict(id='ack_one', kind='ack', corner_type='request_reply', request_ref='req_one'), 'PLAYED')
                    await journal.flush(client)
                    saved = json.loads(await client.hget(prefix + ':request-states', 'req_one'))
                    self.assertEqual(saved['state'], 'played')
                    history = [json.loads(row) for row in await client.lrange(prefix + ':request-history:req_one', 0, -1)]
                    self.assertEqual([row['state'] for row in history if row.get('source') == 'broadcast'],
                                     ['mixing', 'published', 'played'])
                    self.assertEqual(journal.recover(), [])
                finally:
                    journal.close()
        finally:
            for pattern in [prefix + ':*', f'engine:{station}:*']:
                keys = [key async for key in client.scan_iter(match=pattern)]
                if keys:
                    await client.delete(*keys)
            await client.aclose()
