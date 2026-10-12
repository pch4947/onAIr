"""Track reservation and durable clock events against isolated Redis keys."""
import json
import asyncio
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from redis.asyncio import Redis

from app.config import Settings
from app.messaging.consumer import ACCEPT
from app.music import ENQUEUE
from app.streaming.hls import HlsPublisher
from app.music import run_process
from app.streaming.journal import Journal
from test_broadcast_runtime import segment


class TrackClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_future_start_survives_restart_and_is_not_sent_early(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'clock.sqlite'
            music = {**segment('music'), 'music_ref': 'yt:one'}
            journal = Journal(path, 'st_test')
            journal.track_started(music, time.time() + 60)
            journal.track_started(music, time.time())
            journal.close()
            journal = Journal(path, 'st_test')
            try:
                redis = AsyncMock()
                await journal.flush(redis)
                redis.eval.assert_not_awaited()
                self.assertEqual(journal.pending(), 1)
            finally:
                journal.close()

    async def test_intro_mode_waits_for_intro_then_selects_reserved_music(self):
        redis = AsyncMock()
        publisher = HlsPublisher(redis, Settings('127.0.0.1', 3000, track_intro_enabled=True))
        publisher.journal = Mock()
        publisher.journal.status.return_value = None
        music = {**segment('music'), 'id': 'song', 'music_ref': 'yt:one'}
        pipe = AsyncMock()
        pipe.lrange = Mock()
        pipe.hgetall = Mock()
        pipe.__aenter__.return_value = pipe
        pipe.execute.return_value = (['song'], {'song': json.dumps(music)})
        redis.pipeline = Mock(return_value=pipe)
        self.assertIsNone(await publisher.select_next())
        publisher.current = {**segment(), 'id': 'intro', 'music_ref': 'yt:one'}
        redis.hget.return_value = 'song'
        self.assertEqual((await publisher.select_next()).id, 'song')


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class TrackRedisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.station = 'st_track_' + uuid4().hex
        self.prefix = 'backend:' + self.station
        self.redis = Redis.from_url('redis://127.0.0.1:6379/0', decode_responses=True)
        await self.redis.set(self.prefix + ':station', json.dumps(dict(owner_id='owner', status='started')))

    async def asyncTearDown(self):
        keys = [key async for key in self.redis.scan_iter(self.prefix + ':*')]
        keys.append(f'engine:{self.station}:in')
        await self.redis.delete(*keys)
        await self.redis.aclose()

    async def enqueue(self, key, source='playlist'):
        row = {**segment('music'), 'id': key, 'station_id': self.station, 'music_ref': 'yt:' + key}
        meta = dict(track=dict(track_ref=row['music_ref'], title=key, artist='test', duration_ms=8000), source=source)
        keys = [self.prefix + ':' + suffix for suffix in
                ('music-submissions', 'segments', 'queue', 'station', 'tracks', 'track-plan')]
        keys += [f'engine:{self.station}:in', self.prefix + ':playlist', self.prefix + ':playlist-order']
        await self.redis.eval(ENQUEUE, 9, *keys, key, json.dumps(row), json.dumps({'segment_id': key}),
                              'owner', json.dumps(meta), self.station, str(time.time()))
        return row

    async def reserve(self, intro_id, track, row=None):
        keys = [self.prefix + ':' + suffix for suffix in
                ('segments', 'queue', 'tracks', 'track-plan', 'intro-tracks', 'segment-states')]
        return await self.redis.eval(ACCEPT, 6, *keys, intro_id, json.dumps(row or {'id': intro_id}), '1', track)

    async def events(self):
        return [fields for _, fields in await self.redis.xrange(f'engine:{self.station}:in')]

    async def test_replacement_freeze_and_idempotent_enqueue(self):
        await self.enqueue('one')
        await self.enqueue('one')
        await self.enqueue('two', 'request')
        self.assertEqual(len(await self.events()), 2)
        self.assertEqual(await self.reserve('intro', 'yt:two'), 1)
        self.assertEqual(await self.reserve('intro', 'yt:two'), 0)
        self.assertEqual(await self.reserve('duplicate-intro', 'yt:two'), -3)
        await self.enqueue('three', 'vote')
        self.assertEqual(await self.redis.hget(self.prefix + ':track-plan', 'next'), 'two')
        self.assertEqual(len(await self.events()), 2)

    async def test_start_announces_deferred_track_once_and_repeats_playlist(self):
        first = await self.enqueue('one')
        await self.reserve('intro', 'yt:one')
        await self.enqueue('two', 'request')
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / 'clock.sqlite', self.station, repeat_playlist=True)
            try:
                journal.track_started(first, time.time() - 1)
                await journal.flush(self.redis)
                journal.track_started(first, time.time() - 1)
                await journal.flush(self.redis)
                events = await self.events()
                self.assertEqual([e['type'] for e in events], ['track.queued', 'track.started', 'track.queued'])
                self.assertEqual(json.loads(events[-1]['payload'])['track']['track_ref'], 'yt:two')
                self.assertIsNone(await self.redis.hget(self.prefix + ':track-plan', 'frozen'))
                await self.redis.lrem(self.prefix + ':queue', 0, 'one')
                await self.redis.lrem(self.prefix + ':queue', 0, 'two')
                second = {**first, 'id': 'two', 'music_ref': 'yt:two'}
                journal.track_started(second, time.time() - 1)
                await journal.flush(self.redis)
                next_id = await self.redis.hget(self.prefix + ':track-plan', 'next')
                self.assertTrue(next_id.startswith('music_cycle_'))
                saved = json.loads(await self.redis.hget(self.prefix + ':segments', next_id))
                self.assertEqual(saved['music_ref'], 'yt:one')
                # A long playing track can still be MIXING in the Redis queue.
                # A repeated track_ref must reserve the next instance, not it.
                await self.redis.rpush(self.prefix + ':queue', 'one')
                await self.redis.hset(self.prefix + ':track-plan', 'playing', 'one')
                self.assertEqual(await self.reserve('repeat-intro', 'yt:one'), 1)
                self.assertEqual(await self.redis.hget(self.prefix + ':intro-tracks', 'repeat-intro'), next_id)
            finally:
                journal.close()

    async def test_failed_encoding_releases_reservation(self):
        row = await self.enqueue('one')
        await self.reserve('intro', 'yt:one')
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / 'clock.sqlite', self.station)
            try:
                journal.transition(row, 'FAILED')
                await journal.flush(self.redis)
                self.assertIsNone(await self.redis.hget(self.prefix + ':track-plan', 'frozen'))
                self.assertIsNone(await self.redis.hget(self.prefix + ':track-plan', 'next'))
            finally:
                journal.close()

    @unittest.skipUnless(os.environ.get('ONAIR_FFMPEG'), 'Requires FFmpeg')
    async def test_real_hls_publishes_intro_then_music_and_clock_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / self.station / 'a.wav'
            audio.parent.mkdir()
            await run_process([os.environ['ONAIR_FFMPEG'], '-v', 'error', '-f', 'lavfi',
                '-i', 'sine=frequency=440:sample_rate=48000', '-t', '0.5', str(audio)], 10)
            music = await self.enqueue('one')
            music.update(audio_ref=f'{self.station}/a.wav', duration_ms=500)
            await self.redis.hset(self.prefix + ':segments', 'one', json.dumps(music))
            intro = {**music, 'id': 'intro', 'kind': 'speech', 'corner_type': 'music_intro', 'request_ref': None}
            self.assertEqual(await self.reserve('intro', 'yt:one', intro), 1)
            settings = Settings('127.0.0.1', 3000, station_id=self.station, audio_dir=root,
                hls_dir=root / 'hls', ffmpeg=os.environ['ONAIR_FFMPEG'], track_intro_enabled=True,
                broadcast_delay_seconds=1, fallback_mode='silence')
            publisher = HlsPublisher(self.redis, settings)
            try:
                await publisher.start()
                async with asyncio.timeout(20):
                    while not any(e['type'] == 'track.started' for e in await self.events()):
                        await asyncio.sleep(0.1)
                self.assertIsNone(publisher.error)
                rows = list(publisher.journal.db.execute('SELECT id FROM jobs ORDER BY rowid'))
                self.assertEqual([row[0] for row in rows[:2]], ['intro', 'one'])
                starts = [json.loads(e['payload']) for e in await self.events() if e['type'] == 'track.started']
                self.assertEqual(len(starts), 1)
                self.assertEqual(starts[0]['track_ref'], 'yt:one')
                self.assertLessEqual(starts[0]['started_at'], time.time())
                self.assertTrue((publisher.directory / 'index.m3u8').is_file())
            finally:
                await publisher.stop()
