"""Real engine M3 -> Redis -> introduction reservation -> FFmpeg playout.

LLM/TTS use the engine's dummy adapters; no engine submissions are fabricated.
Set ONAIR_E2E_AUDIO_FILE to a previously downloaded real audio file if desired.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from redis.asyncio import Redis

from app.config import Settings
from app.messaging.consumer import EngineConsumer
from app.music import ENQUEUE, MusicService, run_process
from app.stations import StationInput, StationStore
from app.streaming.hls import HlsPublisher
from test_stations import BODY


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1' and os.environ.get('ONAIR_FFMPEG'),
                     'Requires local Redis, engine package and FFmpeg')
class MusicIntroE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_introduces_reserved_track_before_deferred_request(self):
        from onair_engine.engine import EngineSettings
        from onair_engine.manager import EngineManager
        from onair_engine.pipeline.tts import measure_duration_ms
        from onair_engine.transport import RedisTransport

        station = 'st_intro_' + uuid4().hex
        prefix = 'backend:' + station
        redis_url = 'redis://127.0.0.1:6379/15'
        redis = Redis.from_url(redis_url, decode_responses=True)
        publisher = manager = task = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine_root = Path(__file__).resolve().parents[2] / 'engine'
            try:
                source = root / 'audio' / station / 'music.wav'
                source.parent.mkdir(parents=True)
                actual = os.environ.get('ONAIR_E2E_AUDIO_FILE')
                if actual:
                    source = source.with_suffix(Path(actual).suffix)
                    shutil.copyfile(actual, source)
                else:
                    await run_process([os.environ['ONAIR_FFMPEG'], '-v', 'error', '-f', 'lavfi',
                        '-i', 'sine=frequency=440:sample_rate=48000', '-t', '3', str(source)], 15)
                duration = measure_duration_ms(source)
                self.assertIsNotNone(duration)
                # Longer than the engine target AND the 10-second pressure refresh:
                # an unintroduced song must never suppress its own introduction.
                if duration <= 12000 or duration > 20000:
                    extended = source.with_name('long-music.wav')
                    await run_process([os.environ['ONAIR_FFMPEG'], '-v', 'error', '-stream_loop', '-1',
                        '-i', str(source), '-t', '15', str(extended)], 15)
                    source = extended
                    duration = measure_duration_ms(source)
                settings = Settings('127.0.0.1', 3000, station_id=station, redis_url=redis_url,
                    audio_dir=root / 'audio', hls_dir=root / 'hls', ffmpeg=os.environ['ONAIR_FFMPEG'],
                    track_intro_enabled=True, broadcast_delay_seconds=1, fallback_mode='silence')
                store = StationStore(redis, station)
                record = await store.create(StationInput(**(BODY | {'format': 'music'})), 'test-owner', 'test')
                consumer = EngineConsumer(redis, settings)
                task = asyncio.create_task(consumer.run())
                publisher = HlsPublisher(redis, settings)
                await publisher.start()
                manager = EngineManager(EngineSettings(audio_dir=settings.audio_dir,
                    sqlite_path=root / 'engine.sqlite', safety_rules_path=engine_root / 'config/safety_rules.yaml',
                    formats_dir=engine_root / 'config/formats', tts='dummy', llm='dummy',
                    dummy_tracks=False, target_buffer_sec=2),
                    transport_factory=lambda sid: RedisTransport(redis_url, sid, block_ms=100))
                await manager.handle_event({'type': 'station.created', 'station_id': station,
                    'at': record['created_at'], 'payload': record['config']})

                async def enqueue(id, ref, source_kind):
                    row = dict(id=id, station_id=station, audio_ref=source.relative_to(settings.audio_dir).as_posix(),
                        duration_ms=duration, corner_type='music', kind='music', priority=50,
                        reorderable=False, request_ref=None, music_ref=ref, created_at=time.time())
                    meta = dict(track=dict(track_ref=ref, title=id, artist='Local test', duration_ms=duration), source=source_kind)
                    keys = [prefix + ':' + s for s in ('music-submissions', 'segments', 'queue', 'station', 'tracks', 'track-plan')]
                    keys += [f'engine:{station}:in', prefix + ':playlist', prefix + ':playlist-order']
                    await redis.eval(ENQUEUE, 9, *keys, id, json.dumps(row), json.dumps({'segment_id': id}),
                        'test-owner', json.dumps(meta), station, str(time.time()),
                        record['generation'], str(record['created_at']))

                async def wait_for(predicate, label, timeout=100):
                    try:
                        async with asyncio.timeout(timeout):
                            while not await predicate():
                                await asyncio.sleep(.1)
                    except TimeoutError:
                        self.fail('Timed out waiting for ' + label)

                async def started():
                    return json.loads(await redis.get(store.key))['status'] == 'started'
                await wait_for(started, 'engine started')
                await enqueue('music_first', 'local:first', 'playlist')
                async def frozen():
                    return await redis.hget(prefix + ':track-plan', 'frozen') == 'music_first'
                await wait_for(frozen, 'first introduction reservation', 60)
                service = MusicService(redis, settings)
                service.prepare = AsyncMock(return_value=(source, dict(
                    track_ref='yt:BaW_jenozKc', title='신청곡', artist='가수', duration_ms=duration)))
                submitted = await service.enqueue('BaW_jenozKc', 'listener', 'request-key',
                                                  'request', None, '청취자')
                song_id = submitted['segment_id']
                self.assertEqual(json.loads(await redis.hget(prefix + ':requests', song_id))['kind'], 'song_request')
                self.assertEqual(await redis.hget(prefix + ':track-plan', 'next'), 'music_first')
                async def completed():
                    state = await redis.hget(prefix + ':request-states', song_id)
                    return publisher.journal.status(song_id) == 'PLAYED' and state and json.loads(state)['state'] == 'played'
                await wait_for(completed, 'both introductions and tracks completed', 150)

                rows = [json.loads(p) for (p,) in publisher.journal.db.execute('SELECT payload FROM jobs ORDER BY rowid')]
                for id, ref in [('music_first', 'local:first'), (song_id, 'yt:BaW_jenozKc')]:
                    index = next(i for i, row in enumerate(rows) if row['id'] == id)
                    self.assertGreater(index, 0)
                    intro = rows[index - 1]
                    self.assertEqual(intro['kind'], 'speech')
                    self.assertEqual(intro['music_ref'], ref)
                    self.assertEqual(publisher.journal.status(intro['id']), 'PLAYED')
                events = [f for _, f in await redis.xrange(f'engine:{station}:in')]
                starts = [json.loads(f['payload'])['track_ref'] for f in events if f['type'] == 'track.started']
                self.assertEqual(starts[:2], ['local:first', 'yt:BaW_jenozKc'])
                announced = [json.loads(f['payload'])['track']['track_ref'] for f in events if f['type'] == 'track.queued']
                self.assertEqual(announced[:2], ['local:first', 'yt:BaW_jenozKc'])
                song_event = next(json.loads(f['payload']) for f in events if f['type'] == 'track.queued'
                                  and json.loads(f['payload'])['track']['track_ref'] == 'yt:BaW_jenozKc')
                self.assertEqual(song_event['nickname'], '청취자')
                self.assertEqual(await redis.xlen(f'engine:{station}:out:dead'), 0)
                self.assertIsNone(publisher.error)
            finally:
                if manager:
                    await manager.close_all()
                if publisher:
                    await publisher.stop()
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                keys = [k async for k in redis.scan_iter(prefix + ':*')]
                keys += [k async for k in redis.scan_iter(f'engine:{station}:*')]
                if keys:
                    await redis.delete(*keys)
                for id, fields in await redis.xrange('engine:control'):
                    if fields.get('station_id') == station:
                        await redis.xdel('engine:control', id)
                await redis.aclose()
