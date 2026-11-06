"""방 생성 때 고른 첫 노래: 미리 받기, station.created 지연 발행, 방송 시작 시 소개 없이 대기열에 넣기."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings
from app.first_song import FIRST_SONG_SOURCE, enqueue_first_song, ensure_first_song_task, prepare_first_song
from app.messaging.consumer import EngineConsumer
from app.music import MusicService, music_segment_id
from app.stations import StationInput, StationStore

VIDEO = 'dQw4w9WgXcQ'
URL = f'https://youtu.be/{VIDEO}'
BODY = {'broadcast_minutes': 30, 'persona': {
    'style': {'formality': 'polite', 'energy': 'low', 'humor': 'rare'},
    'voice': 'dummy-feminine', 'dj_name': '새벽', 'concept': '스터디 방송',
    'tone': '차분한 존댓말', 'examples': ['안녕하세요.', '반가워요.', '쉬어 가요.']}}
TRACK = {'track_ref': f'yt:{VIDEO}', 'title': '밤편지', 'artist': '아이유', 'duration_ms': 253000}


class FirstSongInputTests(unittest.TestCase):
    def test_link_is_validated_and_kept_out_of_the_engine_payload(self):
        body = StationInput(**(BODY | {'first_song_url': URL}))
        self.assertEqual(body.first_song_url, URL)
        self.assertNotIn('first_song_url', body.legacy_fields())
        for url in ['https://example.com/song', 'not a link']:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                StationInput(**(BODY | {'first_song_url': url}))
        self.assertNotIn('first_song_url', StationInput(**BODY).legacy_fields())


class FirstSongTaskTests(unittest.IsolatedAsyncioTestCase):
    def record(self, **extra):
        return dict(status='preparing', generation='g1', owner_id='u1', first_song_url=URL,
                    first_song_status='preparing', config={}) | extra

    async def test_task_starts_once_and_only_while_waiting_for_the_song(self):
        gate = asyncio.Event()
        state = SimpleNamespace(music=SimpleNamespace(lock=asyncio.Lock(), prepare=AsyncMock(side_effect=gate.wait)),
                                redis=SimpleNamespace(), settings=SimpleNamespace(station_id='st'))
        ensure_first_song_task(state, self.record())
        ensure_first_song_task(state, self.record())
        self.assertEqual(len(state.first_song_tasks), 1)
        for record in [self.record(control_id='1-0'), self.record(status='stopping'),
                       self.record(first_song_status='ready'), {k: v for k, v in self.record().items()
                                                                if k != 'first_song_status'}]:
            fresh = SimpleNamespace()
            ensure_first_song_task(fresh, record)
            self.assertFalse(getattr(fresh, 'first_song_tasks', None))
        state.first_song_tasks['g1'].cancel()

    async def test_started_station_queues_the_cached_song_without_introduction(self):
        music = SimpleNamespace(enqueue=AsyncMock(), is_cached=lambda segment_id: True)
        record = dict(generation='g1', owner_id='u1', config={'first_song': TRACK})
        await enqueue_first_song(SimpleNamespace(music=music), record)
        music.enqueue.assert_awaited_once_with(VIDEO, 'u1', 'first-song-g1', source=FIRST_SONG_SOURCE)
        music.enqueue.reset_mock()
        await enqueue_first_song(SimpleNamespace(music=music), dict(record, config={}))
        music.enqueue.assert_not_awaited()
        for error in [HTTPException(429, 'busy'), RedisError('down'), TimeoutError()]:
            with self.subTest(error=type(error).__name__):
                music.enqueue.side_effect = error
                await enqueue_first_song(SimpleNamespace(music=music), record)  # 실패해도 소비자를 멈추지 않는다

    async def test_missing_audio_is_skipped_instead_of_downloading_in_the_consumer(self):
        music = SimpleNamespace(enqueue=AsyncMock(), is_cached=lambda segment_id: False)
        await enqueue_first_song(SimpleNamespace(music=music),
                                 dict(generation='g1', owner_id='u1', config={'first_song': TRACK}))
        music.enqueue.assert_not_awaited()


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class FirstSongRedisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.station = 'st_first_song_' + uuid.uuid4().hex
        # Separate logical DB: never touch the user's engine:control stream.
        self.redis = Redis.from_url('redis://127.0.0.1:6379/15', decode_responses=True)
        self.store = StationStore(self.redis, self.station)

    async def asyncTearDown(self):
        keys = [k async for k in self.redis.scan_iter(match=f'backend:{self.station}:*')]
        keys += [k async for k in self.redis.scan_iter(match=f'engine:{self.station}:*')]
        if keys:
            await self.redis.delete(*keys)
        for entry_id, fields in await self.redis.xrange('engine:control'):
            if fields.get('station_id') == self.station:
                await self.redis.xdel('engine:control', entry_id)
        await self.redis.aclose()

    async def control_entries(self):
        return [json.loads(f['payload']) for _, f in await self.redis.xrange('engine:control')
                if f.get('station_id') == self.station]

    async def test_creation_waits_for_the_song_then_releases_once(self):
        body = StationInput(**(BODY | {'first_song_url': URL}))
        row = await self.store.create(body, 'u1', 'retry')
        self.assertEqual((row['status'], row['first_song_status']), ('preparing', 'preparing'))
        self.assertNotIn('control_id', row)
        self.assertEqual(await self.control_entries(), [])
        self.assertEqual(await self.store.create(body, 'u1', 'retry'), row)  # same key replays the record
        released = await self.store.release(row['generation'], TRACK)
        self.assertEqual(released['first_song_status'], 'ready')
        self.assertTrue(released['control_id'])
        self.assertEqual(released['config']['first_song'], TRACK)
        self.assertEqual(released['config']['persona']['music_taste'], [])  # empty arrays survive
        entries = await self.control_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['first_song'], TRACK)
        self.assertIsNone(await self.store.release(row['generation'], TRACK))
        self.assertEqual(len(await self.control_entries()), 1)

    async def test_failed_download_starts_without_song_and_stopped_room_is_not_released(self):
        row = await self.store.create(StationInput(**(BODY | {'first_song_url': URL})), 'u1', 'retry')
        music = SimpleNamespace(lock=asyncio.Lock(), prepare=AsyncMock(side_effect=HTTPException(502, 'blocked')))
        state = SimpleNamespace(music=music, redis=self.redis, settings=SimpleNamespace(station_id=self.station))
        await prepare_first_song(state, row)
        entries = await self.control_entries()
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0].get('first_song'))
        record = json.loads(await self.redis.get(self.store.key))
        self.assertEqual(record['first_song_status'], 'failed')

        await self.redis.set(self.store.key, json.dumps(dict(record, status='ended')))
        second = await self.store.create(StationInput(**(BODY | {'first_song_url': URL})), 'u1', 'second')
        await self.redis.set(self.store.key, json.dumps(dict(second, status='stopping')))
        self.assertIsNone(await self.store.release(second['generation'], TRACK))
        self.assertEqual(len(await self.control_entries()), 1)

    async def test_unexpected_download_error_still_starts_the_broadcast(self):
        row = await self.store.create(StationInput(**(BODY | {'first_song_url': URL})), 'u1', 'retry')
        music = SimpleNamespace(lock=asyncio.Lock(), prepare=AsyncMock(side_effect=RuntimeError('worker crashed')))
        state = SimpleNamespace(music=music, redis=self.redis, settings=SimpleNamespace(station_id=self.station))
        await prepare_first_song(state, row)
        self.assertEqual(len(await self.control_entries()), 1)
        self.assertEqual(json.loads(await self.redis.get(self.store.key))['first_song_status'], 'failed')

    async def test_metadata_without_duration_is_rejected_as_a_track(self):
        row = await self.store.create(StationInput(**(BODY | {'first_song_url': URL})), 'u1', 'retry')
        bad = {k: v for k, v in TRACK.items() if k != 'duration_ms'}
        music = SimpleNamespace(lock=asyncio.Lock(), prepare=AsyncMock(return_value=(None, bad)))
        state = SimpleNamespace(music=music, redis=self.redis, settings=SimpleNamespace(station_id=self.station))
        await prepare_first_song(state, row)
        entries = await self.control_entries()
        self.assertIsNone(entries[0].get('first_song'))  # 엔진이 거부할 곡 정보는 보내지 않는다

    async def test_consumer_queues_the_first_song_when_the_engine_starts(self):
        row = await self.store.create(StationInput(**(BODY | {'first_song_url': URL})), 'u1', 'retry')
        released = await self.store.release(row['generation'], TRACK)
        consumer = EngineConsumer(self.redis, Settings('127.0.0.1', 3000, station_id=self.station))
        music = SimpleNamespace(enqueue=AsyncMock(), is_cached=lambda segment_id: True)
        consumer.state = SimpleNamespace(music=music)
        await consumer.ensure_group()
        fields = dict(type='station.started', contract_version='1', event_id='evt_started',
                      station_id=self.station, at=str(released['created_at'] + 1), payload=json.dumps({'topic': 't'}))
        entry_id = await self.redis.xadd(consumer.stream, fields)
        await consumer.handle(entry_id, fields)
        self.assertEqual(json.loads(await self.redis.get(self.store.key))['status'], 'started')
        music.enqueue.assert_awaited_once_with(VIDEO, 'u1', 'first-song-' + row['generation'],
                                               source=FIRST_SONG_SOURCE)

    async def test_first_song_enters_the_queue_without_track_queued(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings('127.0.0.1', 3000, station_id=self.station, audio_dir=Path(directory))
            music = MusicService(self.redis, settings)
            generation = 'g' + uuid.uuid4().hex
            await self.redis.set(f'backend:{self.station}:station', json.dumps(
                dict(status='started', generation=generation, created_at=1.0)))

            async def prepare(video_id, segment_id):
                audio = Path(directory) / self.station / 'youtube' / (segment_id + '.mp3')
                audio.parent.mkdir(parents=True, exist_ok=True)
                audio.write_bytes(b'ID3')
                return audio, dict(TRACK, track_ref='yt:' + video_id)
            music.prepare = prepare

            first = await music.enqueue(VIDEO, 'u1', 'first-song-' + generation, source=FIRST_SONG_SOURCE)
            self.assertEqual(first['segment_id'], music_segment_id(generation, 'u1', 'first-song-' + generation))
            events = [f['type'] for _, f in await self.redis.xrange(f'engine:{self.station}:in')]
            self.assertNotIn('track.queued', events)
            self.assertEqual(await self.redis.lrange(f'backend:{self.station}:queue', 0, -1), [first['segment_id']])
            self.assertEqual(await self.redis.llen(f'backend:{self.station}:playlist-order'), 0)

            await music.enqueue('abcdefghijk', 'u1', 'later', source='playlist')
            events = [f['type'] for _, f in await self.redis.xrange(f'engine:{self.station}:in')]
            self.assertIn('track.queued', events)  # 다른 곡은 지금처럼 엔진에 알린다


if __name__ == '__main__':
    unittest.main()
