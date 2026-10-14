"""Real Redis lifecycle tests; unique station keys, never use the running broadcast."""
import json
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
from fastapi import HTTPException
from redis.asyncio import Redis

from app.config import Settings
from app.station_lifecycle import StationLifecycle
from app.stations import StationStore, StationInput
from app.requests import RequestStore
from app.messaging.consumer import EngineConsumer
from app.music import MusicService
from app.streaming.hls import HlsPublisher
from app.main import create_app
from app.auth import get_current_user
from test_stations import BODY


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires local Redis')
class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sid = 'lifecycle_' + uuid4().hex
        self.redis = Redis.from_url('redis://127.0.0.1:6379/15', decode_responses=True)
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings('127.0.0.1', 3000, station_id=self.sid, hls_dir=Path(self.temp.name))
        self.store = StationStore(self.redis, self.sid)
        self.record = dict(station_id=self.sid, owner_id='owner', status='started', created_at=100,
                           generation=uuid4().hex)
        await self.redis.set(self.store.key, json.dumps(self.record))
        await self.redis.xgroup_create(f'engine:{self.sid}:out', 'backend', id='0', mkstream=True)
        self.publisher = SimpleNamespace(started=True, directory=Path(self.temp.name), stop=AsyncMock(),
            journal=SimpleNamespace(repeat_playlist=True), active_id=None, recovery=[], finishing=[],
            buffer_snapshot=Mock(return_value={'totalAudioEstimateSeconds': 0, 'pendingStateEvents': 0}))
        async def stop_publisher():
            self.publisher.started = False
        self.publisher.stop.side_effect = stop_publisher
        self.http = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200,
            json={'stations': []}, request=httpx.Request('GET', 'http://engine/v1/engine/status'))))
        self.state = SimpleNamespace(redis=self.redis, settings=self.settings, hls=self.publisher,
                                    replay_sessions=None, engine_http=self.http)
        self.lifecycle = StationLifecycle(SimpleNamespace(state=self.state))

    async def asyncTearDown(self):
        keys = [k async for k in self.redis.scan_iter(match=f'*{self.sid}*')]
        if keys:
            await self.redis.delete(*keys)
        for eid, fields in await self.redis.xrange('engine:control'):
            if fields.get('station_id') == self.sid:
                await self.redis.xdel('engine:control', eid)
        await self.redis.aclose()
        self.temp.cleanup()

    async def status(self):
        return json.loads(await self.redis.get(self.store.key))['status']

    async def absent(self):
        for _ in range(3):
            await self.lifecycle.tick()

    async def test_automatic_cleanup_archives_queue_preserves_vod_and_allows_recreation(self):
        prefix = f'backend:{self.sid}'
        await self.redis.rpush(prefix + ':queue', 'old-segment')
        await self.redis.hset(prefix + ':segments', 'old-segment', '{}')
        await self.redis.hset(prefix + ':replay-sessions', 'old-replay', json.dumps({'id': 'old-replay'}))
        request = await RequestStore(self.redis, self.sid).create('hello', 'user', 'story')
        vod = self.publisher.directory / 'vod'
        vod.mkdir()
        (vod / 'recording.ts').write_bytes(b'audio')
        (self.publisher.directory / 'broadcast.sqlite').write_bytes(b'journal')
        await self.absent()
        self.assertEqual(await self.status(), 'ended')
        self.publisher.stop.assert_awaited_once()
        self.assertEqual(await self.redis.llen(prefix + ':queue'), 0)
        self.assertTrue(await self.redis.exists(f'archive:{self.record["generation"]}:{prefix}:queue'))
        self.assertEqual((vod / 'recording.ts').read_bytes(), b'audio')
        self.assertEqual((await RequestStore(self.redis, self.sid).get(request['id']))['status'], 'failed')
        self.assertTrue(await self.redis.hexists(prefix + ':segments', 'old-segment'))
        self.assertEqual(json.loads(await self.redis.hget(prefix + ':replay-sessions', 'old-replay'))['ownerId'], 'owner')
        self.assertFalse(await self.redis.exists(f'engine:{self.sid}:in'))
        self.assertTrue(await self.redis.exists(f'archive:{self.record["generation"]}:engine:{self.sid}:in'))
        created = await self.store.create(StationInput(**BODY), 'another-owner', 'new-broadcast')
        self.assertEqual(created['status'], 'preparing')
        self.assertNotEqual(created['generation'], self.record['generation'])

    async def test_network_errors_never_mean_ended(self):
        self.http.get.side_effect = httpx.ConnectError('offline')
        for _ in range(4):
            with self.assertRaises(httpx.ConnectError):
                await self.lifecycle.tick()
        self.assertEqual(await self.status(), 'started')
        self.publisher.stop.assert_not_awaited()

    async def test_owner_and_generation_guard_and_idempotent_stop(self):
        for owner, created, status in [('visitor', 100, 404), ('owner', 101, 409)]:
            with self.assertRaises(HTTPException) as caught:
                await self.lifecycle.request_stop(owner, created)
            self.assertEqual(caught.exception.status_code, status)
        first = await self.lifecycle.request_stop('owner', 100)
        second = await self.lifecycle.request_stop('owner', 100)
        self.assertEqual(first, second)
        await self.absent()
        await self.lifecycle.tick()
        self.assertFalse(self.publisher.started)
        self.assertEqual(await self.status(), 'ended')

    async def test_manual_stop_delivers_engine_event_before_cleanup(self):
        await self.lifecycle.request_stop('owner', 100)
        self.http.get.return_value = httpx.Response(200, json={'stations': [{'station_id': self.sid}]},
            request=httpx.Request('GET', 'http://engine/v1/engine/status'))
        await self.lifecycle.tick()
        entries = await self.redis.xrange(f'engine:{self.sid}:in')
        self.assertEqual(entries[-1][1]['type'], 'station.closed')
        self.assertEqual(await self.status(), 'stopping')
        self.publisher.stop.assert_awaited_once()

    async def test_drain_waits_for_audio_and_pending_engine_messages(self):
        self.state.settings = SimpleNamespace(hls_enabled=True)
        self.publisher.buffer_snapshot.return_value['totalAudioEstimateSeconds'] = 8
        await self.absent()
        self.assertEqual(await self.status(), 'draining')
        self.assertFalse(self.publisher.journal.repeat_playlist)
        self.publisher.stop.assert_not_awaited()
        self.publisher.buffer_snapshot.return_value['totalAudioEstimateSeconds'] = 0
        await self.redis.xadd(f'engine:{self.sid}:out', {'type': 'test'})
        await self.lifecycle.tick()
        self.publisher.stop.assert_not_awaited()

    async def test_unconsumed_station_creation_is_not_cleaned(self):
        await self.redis.delete(self.store.key)
        await self.store.create(StationInput(**BODY), 'owner', 'pending')
        await self.absent()
        self.assertEqual(await self.status(), 'preparing')

    async def test_cleaning_marker_resumes_after_restart(self):
        await self.redis.set(self.store.key, json.dumps(self.record | {'status': 'cleaning'}))
        await self.lifecycle.tick()
        self.assertEqual(await self.status(), 'ended')
        self.http.get.assert_not_awaited()

    async def test_requests_are_rejected_while_stopping(self):
        await self.lifecycle.request_stop('owner', 100)
        with self.assertRaises(HTTPException) as caught:
            await RequestStore(self.redis, self.sid).create('too late', 'listener', 'story')
        self.assertEqual(caught.exception.status_code, 409)

    async def test_old_engine_result_does_not_touch_replacement_station(self):
        await self.redis.set(self.store.key, json.dumps(self.record | {'status': 'preparing'}))
        consumer = EngineConsumer(self.redis, self.settings)
        fields = {'type': 'station.started', 'contract_version': '1', 'event_id': 'evt_old',
            'station_id': self.sid, 'at': '99', 'payload': json.dumps({'topic': 'old'})}
        eid = await self.redis.xadd(consumer.stream, fields)
        await consumer.handle(eid, fields)
        self.assertEqual(await self.status(), 'preparing')

    async def test_stop_http_auth_and_owner_checks(self):
        app = create_app(self.settings)
        app.state.lifecycle = self.lifecycle
        app.state.hls = SimpleNamespace(ready=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            path = f'/api/stations/{self.sid}/stop'
            self.assertEqual((await client.post(path, json={'created_at': 100})).status_code, 401)
            app.dependency_overrides[get_current_user] = lambda: {'id': 'visitor'}
            self.assertEqual((await client.post(path, json={'created_at': 100})).status_code, 404)
            app.dependency_overrides[get_current_user] = lambda: {'id': 'owner'}
            self.assertEqual((await client.post(path, json={'created_at': 99})).status_code, 409)
            response = await client.post(path, json={'created_at': 100})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()['station']['status'], 'stopping')

    async def test_download_cannot_enter_replacement_broadcast(self):
        import wave
        settings = replace(self.settings, audio_dir=Path(self.temp.name))
        service = MusicService(self.redis, settings)
        async def prepare(video_id, segment_id):
            audio = Path(self.temp.name) / self.sid / 'track.wav'
            audio.parent.mkdir()
            with wave.open(str(audio), 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(48000)
                output.writeframes(b'\0\0' * 48000)
            await self.redis.set(self.store.key, json.dumps(self.record | {
                'created_at': 101, 'generation': uuid4().hex}))
            return audio, {'track_ref': 'yt:' + video_id, 'title': 'test', 'artist': 'test', 'duration_ms': 1000}
        service.prepare = prepare
        with self.assertRaises(HTTPException) as caught:
            await service.enqueue('abcdefghijk', 'listener', 'retry')
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(await self.redis.llen(f'backend:{self.sid}:queue'), 0)

    @unittest.skipUnless(os.environ.get('ONAIR_FFMPEG'), 'Requires FFmpeg')
    async def test_real_publisher_finalizes_vod_and_starts_fresh_session(self):
        settings = replace(self.settings, hls_enabled=True, ffmpeg=os.environ['ONAIR_FFMPEG'],
                           fallback_mode='silence')
        self.state.settings = settings
        publisher = HlsPublisher(self.redis, settings)
        self.state.hls = publisher
        try:
            await publisher.start()
            async with asyncio.timeout(15):
                while not publisher.ready:
                    await asyncio.sleep(.1)
            session = publisher.session
            await self.lifecycle.request_stop('owner', 100)
            await self.absent()
            self.assertEqual(await self.status(), 'ended')
            manifest = publisher.directory / 'vod' / session / 'index.m3u8'
            self.assertIn('#EXT-X-ENDLIST', manifest.read_text())
            self.assertTrue(list(publisher.directory.glob('ended-*.sqlite')))
            await self.store.create(StationInput(**BODY), 'owner', 'new')
            await self.lifecycle.tick()
            self.assertTrue(self.state.hls.started)
            self.assertNotEqual(self.state.hls.session, session)
            self.assertEqual(self.state.hls.journal.recover(), [])
        finally:
            await self.state.hls.stop()
