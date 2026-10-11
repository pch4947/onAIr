"""Station API permissions, Redis atomic creation, and engine lifecycle round trip."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import uuid

import httpx
from redis.asyncio import Redis
from redis.exceptions import ResponseError, ConnectionError
from fastapi import HTTPException

from app.auth import get_current_user
from app.config import Settings
from app.main import create_app
from app.messaging.consumer import EngineConsumer
from app.stations import StationInput, StationStore, engine_call


BODY = {'broadcast_minutes': 30, 'persona': {
    'style': {'formality': 'polite', 'energy': 'low', 'humor': 'rare'},
    'voice': 'dummy-feminine', 'dj_name': '새벽', 'concept': '스터디 방송',
    'tone': '차분한 존댓말', 'examples': ['안녕하세요.', '반가워요.', '쉬어 가요.']}}


class StationApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_lookup_and_redis_failure(self):
        app = create_app(Settings('127.0.0.1', 3000))
        app.dependency_overrides[get_current_user] = lambda: {'id': 'u1'}
        app.state.hls = SimpleNamespace(ready=True)
        app.state.stations = SimpleNamespace(key='station')
        app.state.redis = SimpleNamespace(get=AsyncMock(return_value=json.dumps({
            'station_id': 'st_local_dev', 'owner_id': 'u2', 'status': 'started'})))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            self.assertEqual((await client.get('/api/stations/st_local_dev')).status_code, 404)
            app.state.redis.get.return_value = json.dumps({
                'station_id': 'st_local_dev', 'owner_id': 'u1', 'status': 'started'})
            self.assertEqual((await client.get('/api/stations/st_local_dev')).status_code, 200)
            app.state.redis.get.side_effect = ConnectionError('offline')
            self.assertEqual((await client.get('/api/stations/st_local_dev')).status_code, 503)

    async def test_proxy_timeout_unavailable_and_validation(self):
        upstream = SimpleNamespace(request=AsyncMock())
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine_http=upstream)))
        for exc, status in [(httpx.ReadTimeout('private'), 504), (httpx.ConnectError('private'), 503)]:
            upstream.request.side_effect = exc
            with self.assertRaises(HTTPException) as caught:
                await engine_call(request, 'GET', '/v1/voices')
            self.assertEqual(caught.exception.status_code, status)
            self.assertNotIn('private', str(caught.exception.detail))
        upstream.request.side_effect = None
        upstream.request.return_value = httpx.Response(422, json={'errors': [{'field': 'voice', 'reason': 'unknown'}]})
        with self.assertRaises(HTTPException) as caught:
            await engine_call(request, 'POST', '/v1/personas/check')
        self.assertEqual(caught.exception.detail['errors'][0]['field'], 'voice')

    async def test_auth_validation_proxy_errors_and_readiness(self):
        app = create_app(Settings('127.0.0.1', 3000))
        app.state.hls = SimpleNamespace(ready=True)
        app.state.stations = SimpleNamespace(create=AsyncMock(return_value={
            'station_id': 'st_local_dev', 'status': 'preparing', 'owner_id': 'u1',
            'retry_key': 'private', 'fingerprint': 'private'}))
        app.state.engine_http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={'voices': [{'id': 'dummy'}]})),
            base_url='http://engine')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            self.assertEqual((await client.get('/api/voices')).status_code, 401)
            app.dependency_overrides[get_current_user] = lambda: {'id': 'u1'}
            self.assertEqual((await client.post('/api/stations', json=BODY)).status_code, 422)
            headers = {'Idempotency-Key': 'one'}
            self.assertEqual((await client.post('/api/stations', json={**BODY, 'first_song': {}}, headers=headers)).status_code, 422)
            result = await client.post('/api/stations', json=BODY, headers=headers)
            self.assertEqual(result.status_code, 202)
            self.assertNotIn('private', result.text)
            self.assertEqual(result.json()['station']['status'], 'preparing')
            self.assertFalse(result.json()['station']['hls_ready'])  # HLS disabled
            self.assertEqual((await client.get('/api/voices')).json()['voices'][0]['id'], 'dummy')
            await app.state.engine_http.aclose()
            app.state.engine_http = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda req: httpx.Response(401, text='secret-token')), base_url='http://engine')
            result = await client.get('/api/voices')
            self.assertEqual(result.status_code, 502)
            self.assertNotIn('secret-token', result.text)
        await app.state.engine_http.aclose()


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class StationRedisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.station = 'st_creation_' + uuid.uuid4().hex
        # Separate logical DB: never clean or consume the user's engine:control stream.
        self.redis = Redis.from_url('redis://127.0.0.1:6379/15', decode_responses=True)
        self.store = StationStore(self.redis, self.station)

    async def asyncTearDown(self):
        keys = [k async for k in self.redis.scan_iter(match=f'backend:{self.station}:*')]
        keys += [k async for k in self.redis.scan_iter(match=f'engine:{self.station}:*')]
        if keys:
            await self.redis.delete(*keys)
        # Delete only control entries for this test station, never the shared stream.
        for entry_id, fields in await self.redis.xrange('engine:control'):
            if fields.get('station_id') == self.station:
                await self.redis.xdel('engine:control', entry_id)
        await self.redis.aclose()

    async def test_concurrent_creation_and_actual_engine_started(self):
        body = StationInput.model_validate(BODY)
        records = await asyncio.gather(*(self.store.create(body, 'u1', 'retry') for _ in range(4)))
        self.assertTrue(all(row == records[0] for row in records))
        entries = [(eid, fields) for eid, fields in await self.redis.xrange('engine:control')
                   if fields.get('station_id') == self.station]
        self.assertEqual(len(entries), 1)
        with self.assertRaises(HTTPException) as conflict:
            await self.store.create(body, 'u2', 'retry')
        self.assertEqual(conflict.exception.status_code, 409)
        from onair_engine.engine import EngineSettings
        from onair_engine.manager import EngineManager
        from onair_engine.transport import RedisTransport
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = EngineSettings(audio_dir=root / 'audio', sqlite_path=root / 'metrics.sqlite',
                safety_rules_path=Path(__file__).resolve().parents[2] / 'engine/config/safety_rules.yaml')
            manager = EngineManager(settings, transport_factory=lambda sid:
                RedisTransport('redis://127.0.0.1:6379/15', sid))
            consumer = EngineConsumer(self.redis, Settings('127.0.0.1', 3000, station_id=self.station))
            try:
                fields = entries[0][1]
                await manager.handle_event({'type': fields['type'], 'station_id': self.station,
                                            'payload': json.loads(fields['payload']), 'at': float(fields['at'])})
                async with asyncio.timeout(15):
                    while True:
                        messages = await self.redis.xrange(f'engine:{self.station}:out')
                        started = [(eid, f) for eid, f in messages if f['type'] == 'station.started']
                        if started:
                            break
                        await asyncio.sleep(0.1)
                await consumer.ensure_group()
                await consumer.handle(*started[0])
                await consumer.handle(*started[0])
                record = json.loads(await self.redis.get(self.store.key))
                self.assertEqual(record['status'], 'started')
                self.assertEqual(record['engine_result']['topic'], BODY['persona']['concept'])
            finally:
                await manager.close_all()

    async def test_rejection_stale_event_and_wrong_key_no_partial_write(self):
        await self.redis.set(f'backend:{self.station}:personas', 'wrong-type')
        with self.assertRaises(ResponseError):
            await self.store.create(StationInput.model_validate(BODY), 'u1', 'retry')
        self.assertIsNone(await self.redis.get(self.store.key))
        await self.redis.delete(f'backend:{self.station}:personas')
        row = await self.store.create(StationInput.model_validate(BODY), 'u1', 'retry')
        await self.store.result('station.started', {'topic': 'old'}, row['created_at'] - 1)
        self.assertEqual(json.loads(await self.redis.get(self.store.key))['status'], 'preparing')
        consumer = EngineConsumer(self.redis, Settings('127.0.0.1', 3000, station_id=self.station))
        await consumer.ensure_group()
        fields = dict(type='station.rejected', contract_version='1', event_id='evt_test',
            station_id=self.station, at=str(row['created_at'] + 1),
            payload=json.dumps({'reason': 'unknown_voice', 'detail': 'Unknown voice'}))
        eid = await self.redis.xadd(consumer.stream, fields)
        await consumer.handle(eid, fields)
        self.assertEqual(json.loads(await self.redis.get(self.store.key))['status'], 'rejected')
