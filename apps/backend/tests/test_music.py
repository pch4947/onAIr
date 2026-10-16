"""YouTube boundary validation, permissions, idempotency and local Redis/HLS integration."""
import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fastapi import HTTPException
from redis.asyncio import Redis

from app.auth import get_current_user
from app.config import Settings
from app.main import create_app
from app.music import MusicService, run_process, youtube_id
from app.streaming.hls import HlsPublisher
from app.youtube_worker import validate_metadata

VIDEO = 'BaW_jenozKc'


class ValidationTests(unittest.TestCase):
    def test_only_canonicalizable_video_links(self):
        for url in [f'https://youtu.be/{VIDEO}?t=10', f'https://www.youtube.com/watch?v={VIDEO}&list=abc',
                    f'https://music.youtube.com/watch?v={VIDEO}', f'https://youtube.com/shorts/{VIDEO}']:
            self.assertEqual(youtube_id(url), VIDEO)
        for url in ['http://youtube.com/watch?v=' + VIDEO, 'https://youtube.com.evil.test/watch?v=' + VIDEO,
                    'https://localhost/watch?v=' + VIDEO, 'file:///secret', '노래 제목',
                    'https://youtube.com/playlist?list=abc', f'https://user@youtube.com/watch?v={VIDEO}',
                    f'https://youtube.com:9000/watch?v={VIDEO}', f'https://youtu.be/{VIDEO}/extra']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                youtube_id(url)

    def test_reject_live_age_restricted_unknown_or_long_duration(self):
        info = dict(id=VIDEO, title='test', uploader='artist', duration=5)
        self.assertEqual(validate_metadata(info, VIDEO, 600)['duration_ms'], 5000)
        for changes in [dict(is_live=True), dict(age_limit=18), dict(duration=None),
                        dict(duration=601), dict(duration=float('nan')), dict(id='other'),
                        dict(live_status='is_upcoming'), dict(_type='playlist')]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_metadata(info | changes, VIDEO, 600)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_listener_state_and_accepted_queue(self):
        app = create_app(Settings('127.0.0.1', 3000, hls_enabled=True))
        app.state.redis = SimpleNamespace(get=AsyncMock(return_value=json.dumps(dict(owner_id='owner', status='started'))))
        app.state.music = SimpleNamespace(enqueue=AsyncMock(return_value={'status': 'queued'}))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            path = '/api/stations/st_local_dev/tracks'
            body = {'url': f'https://youtu.be/{VIDEO}'}
            headers = {'Idempotency-Key': 'song-one'}
            self.assertEqual((await client.post(path, json=body, headers=headers)).status_code, 401)
            async def current_listener():
                return {'id': 'other'}
            app.dependency_overrides[get_current_user] = current_listener
            self.assertEqual((await client.post(path, json={'url': 'https://example.com'}, headers=headers)).status_code, 422)
            self.assertEqual((await client.post(path, json=body)).status_code, 422)
            app.state.music.enqueue.assert_not_awaited()
            self.assertEqual((await client.post(path, json=body, headers=headers)).status_code, 202)
            app.state.music.enqueue.assert_awaited_once_with(VIDEO, 'other', 'song-one')
            app.state.redis.get.return_value = json.dumps(dict(owner_id='owner', status='preparing'))
            self.assertEqual((await client.post(path, json=body, headers=headers)).status_code, 409)

    async def test_listener_access_exposes_only_readiness(self):
        app = create_app(Settings('127.0.0.1', 3000))
        app.state.redis = SimpleNamespace(get=AsyncMock(return_value=json.dumps(
            dict(owner_id='private-owner', status='started', config={'persona': 'private'}))))
        path = '/api/stations/st_local_dev/tracks/access'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            self.assertEqual((await client.get(path)).status_code, 401)
            async def current_listener():
                return {'id': 'listener'}
            app.dependency_overrides[get_current_user] = current_listener
            self.assertEqual((await client.get(path)).json(), {'can_request': True})
            app.state.redis.get.return_value = json.dumps({'status': 'ended'})
            self.assertEqual((await client.get(path)).json(), {'can_request': False})
            app.state.redis.get.return_value = None
            self.assertEqual((await client.get(path)).status_code, 404)

    async def test_queue_is_public_and_keeps_only_music_segments_in_order(self):
        app = create_app(Settings('127.0.0.1', 3000))
        music1 = {'segment_id': 'music_a', 'track': {'title': '곡 A', 'artist': 'X', 'duration_ms': 1000}}
        music2 = {'segment_id': 'music_b', 'track': {'title': '곡 B', 'artist': 'Y', 'duration_ms': 2000}}
        submissions = {'music_a': json.dumps(music1), 'music_b': json.dumps(music2)}
        app.state.redis = SimpleNamespace(
            lrange=AsyncMock(return_value=['speech_1', 'music_a', 'speech_2', 'music_b']),
            hmget=AsyncMock(side_effect=lambda key, ids: [submissions.get(i) for i in ids]),
            hget=AsyncMock(return_value='music_a'))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            res = await client.get('/api/stations/st_local_dev/tracks/queue')
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual([item['segment_id'] for item in body['queue']], ['music_a', 'music_b'])
            self.assertEqual(body['playingSegmentId'], 'music_a')

    async def test_queue_unknown_station_is_404(self):
        app = create_app(Settings('127.0.0.1', 3000))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            res = await client.get('/api/stations/other-station/tracks/queue')
            self.assertEqual(res.status_code, 404)

    async def test_busy_retry_and_failed_download_never_enqueues(self):
        redis = SimpleNamespace(hget=AsyncMock(return_value=None), eval=AsyncMock(),
                                get=AsyncMock(return_value=json.dumps({'status': 'started'})))
        service = MusicService(redis, Settings('127.0.0.1', 3000))
        await service.lock.acquire()
        with self.assertRaises(HTTPException) as error:
            await service.enqueue(VIDEO, 'owner', 'one')
        self.assertEqual(error.exception.status_code, 429)
        service.lock.release()
        service.prepare = AsyncMock(side_effect=TimeoutError)
        with self.assertRaises(TimeoutError):
            await service.enqueue(VIDEO, 'owner', 'one')
        redis.eval.assert_not_awaited()
        self.assertFalse(service.lock.locked())
        redis.hget.return_value = json.dumps({'track': {'track_ref': 'yt:' + VIDEO}})
        await service.enqueue(VIDEO, 'owner', 'one')
        service.prepare.assert_awaited_once()
        with self.assertRaises(HTTPException) as error:
            await service.enqueue('abcdefghijk', 'owner', 'one')
        self.assertEqual(error.exception.status_code, 409)

    async def test_process_timeout_and_cancellation_reap_child(self):
        for cancelled in (False, True):
            proc = SimpleNamespace(returncode=None, wait=AsyncMock(), kill=lambda: setattr(proc, 'returncode', -1))
            async def wait():
                if proc.returncode is None:
                    await asyncio.sleep(10)
            proc.wait.side_effect = wait
            with patch('app.music.asyncio.create_subprocess_exec', AsyncMock(return_value=proc)):
                task = asyncio.create_task(run_process(['dummy'], 0.01 if not cancelled else 10))
                if cancelled:
                    await asyncio.sleep(0)
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancelled else TimeoutError):
                    await task
            self.assertEqual(proc.returncode, -1)
            self.assertEqual(proc.wait.await_count, 2)


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1' and os.environ.get('ONAIR_FFMPEG'),
                     'Requires Redis and FFmpeg; no external YouTube connection')
class MusicIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_boundary_to_redis_hls_and_idempotent_retry(self):
        station = 'st_music_' + uuid4().hex
        redis = Redis.from_url('redis://127.0.0.1:6379/0', decode_responses=True)
        prefix = 'backend:' + station
        publisher = None
        try:
            await redis.set(prefix + ':station', json.dumps(dict(owner_id='owner', status='started')))
            async with AsyncExitStack() as stack:
                temp = stack.enter_context(tempfile.TemporaryDirectory())
                root = Path(temp)
                settings = Settings('127.0.0.1', 3000, station_id=station, audio_dir=root / 'audio',
                    hls_dir=root / 'hls', ffmpeg=os.environ['ONAIR_FFMPEG'], hls_enabled=True,
                    broadcast_delay_seconds=1, fallback_mode='silence', lavalink_password='test')
                service = MusicService(redis, settings)
                original = run_process
                async def local_download(args, timeout, **kwargs):
                    if args[0] == sys.executable:
                        directory = Path(args[4])
                        await original([settings.ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi',
                            '-i', 'sine=frequency=440:sample_rate=48000', '-t', '1', str(directory / 'source.wav')], 15)
                        (directory / 'result.json').write_text(json.dumps(dict(source='source.wav',
                            track_ref='yt:' + VIDEO, title='local fixture', artist='test', duration_ms=1000)), encoding='utf-8')
                    else:
                        await original(args, timeout)
                with patch('app.music.run_process', side_effect=local_download):
                    first = await service.enqueue(VIDEO, 'listener', 'one')
                    second = await service.enqueue(VIDEO, 'listener', 'one')
                self.assertEqual(first, second)
                self.assertEqual(await redis.llen(prefix + ':queue'), 1)
                publisher = HlsPublisher(redis, settings)
                stack.push_async_callback(publisher.stop)
                await publisher.start()
                async with asyncio.timeout(20):
                    while (not publisher.ready
                           or publisher.journal.status(first['segment_id']) not in ('PUBLISHED', 'PLAYED')):
                        await asyncio.sleep(0.1)
                manifest = (publisher.directory / 'index.m3u8').read_text()
                self.assertIn('#EXTINF:', manifest)
                fragment = next(name for name in manifest.splitlines() if name.endswith('.ts')
                    and (publisher.playback_metadata(name)['segment'] or {}).get('id') == first['segment_id'])
                app = create_app(settings)
                app.state.hls = publisher
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
                    playlist_response = await client.get(first['hls_url'])
                    self.assertEqual(playlist_response.status_code, 200)
                    self.assertIn(fragment, playlist_response.text)
                    audio_response = await client.get(f'/hls/{station}/{fragment}')
                    self.assertEqual(audio_response.status_code, 200)
                    self.assertGreater(len(audio_response.content), 0)
                await original([settings.ffmpeg, '-nostdin', '-v', 'error', '-i',
                                str(publisher.directory / fragment), '-f', 'null', '-'], 15)
                self.assertEqual((await service.enqueue(VIDEO, 'listener', 'one'))['segment_id'], first['segment_id'])
                async with asyncio.timeout(5):
                    while await redis.llen(prefix + ':queue'):
                        await asyncio.sleep(0.1)
        finally:
            for pattern in (prefix + ':*', 'engine:' + station + ':*'):
                keys = [key async for key in redis.scan_iter(match=pattern)]
                if keys:
                    await redis.delete(*keys)
            await redis.aclose()
