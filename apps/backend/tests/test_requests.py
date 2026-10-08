"""HTTP request persistence, idempotency, and real engine event round trip."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler
import uuid

from redis import Redis


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires local Redis and engine')
class RequestIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.station = 'st_requests_' + uuid.uuid4().hex
        self.prefix = f'backend:{self.station}'
        self.redis = Redis.from_url('redis://127.0.0.1:6379/0', decode_responses=True)
        self.addCleanup(self.redis.close)
        self.redis.ping()
        self.addCleanup(self.clean_keys)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.base = f'http://127.0.0.1:{self.port}'
        self.start()
        self.addCleanup(self.stop)

    def clean_keys(self):
        for pattern in [f'{self.prefix}:*', f'engine:{self.station}:*']:
            keys = list(self.redis.scan_iter(match=pattern))
            if keys:
                self.redis.delete(*keys)

    def start(self):
        self.server = subprocess.Popen([sys.executable, '-m', 'app'],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=dict(os.environ, HOST='127.0.0.1', PORT=str(self.port), ONAIR_HLS_ENABLED='0',
                     ONAIR_REDIS_URL='redis://127.0.0.1:6379/0', ONAIR_STATION_ID=self.station))
        for _ in range(100):
            try:
                if self.call('/health')[0] == 200:
                    return
            except URLError:
                time.sleep(0.1)
        self.stop()
        self.fail('Backend startup timeout')

    def stop(self):
        self.server.terminate()
        self.server.wait(timeout=10)

    def call(self, path, body=None, key=None):
        headers = {'Content-Type': 'application/json'}
        if key is not None:
            headers['Idempotency-Key'] = key
        request = Request(self.base + path, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        try:
            response = build_opener(ProxyHandler({})).open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_delivery_retry_restart_and_engine_state(self):
        body = {'prompt': '차분한 분위기로 부탁해요', 'kind': 'mood'}
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.call('/api/requests', body, 'retry-one'), range(4)))
        self.assertTrue(all(status == 202 for status, _ in replies))
        entries = [reply['request'] for _, reply in replies]
        self.assertTrue(all(entry == entries[0] for entry in entries))
        entry = entries[0]
        self.assertEqual(entry['status'], 'requested')
        self.assertEqual(entry['listenerId'], 'anonymous')
        self.assertEqual(self.redis.xlen(f'engine:{self.station}:in'), 1)
        fields = self.redis.xrange(f'engine:{self.station}:in')[0][1]
        self.assertEqual(fields['contract_version'], '1')
        self.assertEqual(fields['station_id'], self.station)
        self.assertEqual(json.loads(fields['payload']), dict(request_id=entry['id'], kind='mood',
            body=body['prompt'], requester_ref='anonymous'))
        self.assertEqual(self.call('/api/requests', {'prompt': '다른 내용'}, 'retry-one')[0], 409)
        self.stop()
        self.start()
        self.assertEqual(self.call('/api/requests', body, 'retry-one')[1]['request'], entry)
        self.assertEqual(self.redis.xlen(f'engine:{self.station}:in'), 1)
        self.assertEqual(self.call('/api/requests')[1]['requests'], [entry])
        self.assertEqual(self.call('/api/requests/' + entry['id'] + '/state')[1]['state'], 'requested')
        self.assertEqual(self.call('/api/stream/state')[1]['pendingRequestCount'], 1)

        async def engine_round_trip():
            from onair_engine.main import load_config
            from onair_engine.engine import StationEngine
            from onair_engine.manager import EngineManager
            from onair_engine.transport import RedisTransport
            engine_dir = Path(__file__).resolve().parents[2] / 'engine'
            config, settings = load_config(engine_dir / 'config/station.example.yaml')
            config.station_id = self.station
            with tempfile.TemporaryDirectory() as directory:
                settings = replace(settings, sqlite_path=Path(directory) / 'metrics.sqlite',
                    audio_dir=Path(directory), tts_cache_dir=None,
                    safety_rules_path=engine_dir / 'config/safety_rules.yaml', llm='dummy', tts='dummy')
                transport = RedisTransport('redis://127.0.0.1:6379/0', self.station)
                engine = StationEngine(config, settings, transport)
                events = transport.events()
                try:
                    event = await asyncio.wait_for(anext(events), 5)
                    await EngineManager(settings)._route_station_event(engine, event)
                    self.assertEqual(engine.scheduler._pending[0].request_id, entry['id'])
                    # SCREENED is emitted by real submit_request.
                finally:
                    # No generation or external LLM/TTS call is needed for request delivery.
                    await events.aclose()
                    await transport.close()
                    engine.telemetry.close()
        asyncio.run(engine_round_trip())
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            state = self.call('/api/requests/' + entry['id'] + '/state')[1]['state']
            if state == 'screened':
                break
            time.sleep(0.1)
        self.assertEqual(state, 'screened')
        self.assertEqual(self.call('/api/requests')[1]['requests'][0]['status'], state)
        # Consumer state ordering is also covered by test_consumer. Check unified metrics on rejection.
        self.redis.xadd(f'engine:{self.station}:out', dict(type='request.state', contract_version='1',
            station_id=self.station, event_id='evt_rejected', at=str(time.time()),
            payload=json.dumps(dict(request_id=entry['id'], state='rejected'))))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.call('/api/stream/state')[1]['pendingRequestCount'] == 0:
                break
            time.sleep(0.1)
        self.assertEqual(self.call('/api/stream/state')[1]['pendingRequestCount'], 0)
        self.assertEqual(self.call('/api/requests/' + entry['id'] + '/state')[1]['state'], 'rejected')

    def test_validation_and_no_partial_write_on_wrong_key_type(self):
        for body in [{'prompt': '  '}, {'prompt': 'x', 'kind': 'song'}, {'prompt': 'x' * 4001}]:
            self.assertEqual(self.call('/api/requests', body)[0], 422)
        self.assertEqual(self.call('/api/requests', {'prompt': 'x'}, 'bad key')[0], 422)
        self.redis.set(f'engine:{self.station}:in', 'wrong-type')
        self.assertEqual(self.call('/api/requests', {'prompt': 'x'}, 'retry')[0], 503)
        self.assertFalse(self.redis.exists(self.prefix + ':requests'))
        self.assertFalse(self.redis.exists(self.prefix + ':request-keys'))
