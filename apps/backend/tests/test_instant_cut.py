"""테스트 방송 버튼의 재생 중단 (#314): 재생 중인 조각을 끊고, 준비한 다음 멘트를 놓아 준다. 실제 방송은 끊지 않는다."""
import asyncio
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock

import httpx
from redis.asyncio import Redis

from app.auth import get_current_user
from app.config import Settings
from app.main import create_app
from app.messaging.contracts import Segment
from app.station_runtime import default_runtime
from app.streaming.hls import HlsPublisher
from app.streaming.journal import Journal
from test_broadcast_runtime import segment
from test_test_controls import publisher as fake_publisher, segment as queued


class CutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.publisher = HlsPublisher(None, Settings('127.0.0.1', 3000, hls_dir=root))
        self.publisher.directory.mkdir()
        self.publisher.journal = Journal(root / 'journal.sqlite', 'st_test')
        self.seen = []
        self.rows = []  # 준비할 조각 (id, kind, 조각 수)
        self.publisher.prepare_next = self.prepare
        self.publisher.publish = self.publish
        self.publisher.fallback_chunks = AsyncMock(return_value=[('fallback', 0.05)])

    async def asyncTearDown(self):
        if self.publisher.task:
            self.publisher.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.publisher.task
        if self.publisher.preparation:
            self.publisher.preparation.cancel()
        self.publisher.journal.close()
        self.directory.cleanup()

    async def prepare(self):
        if self.publisher.holds or not self.rows:
            return None
        key, kind, count = self.rows.pop(0)
        row = {**segment(kind), 'id': key}
        self.publisher.preparing = Segment.model_validate(row)
        self.publisher.journal.transition(row, 'MIXING')
        if key.startswith('slow_'):
            await self.encoding.wait()
        chunks = [(f'{key}_{index}.ts', 0.2) for index in range(count)]
        for name, _ in chunks:
            (self.publisher.directory / name).write_bytes(b'audio')
        self.publisher.unpublished_seconds += 0.2 * count
        self.publisher.active_id = key
        return row, chunks

    def publish(self, name, duration, boundary):
        self.seen.append(self.publisher.current['id'] if self.publisher.current else 'fallback')

    async def play_until(self, count):
        self.publisher.task = asyncio.create_task(self.publisher.run())
        async with asyncio.timeout(3):
            while len(self.seen) < count:
                await asyncio.sleep(0.01)

    async def test_cut_stops_the_playing_segment_and_releases_the_prepared_speech(self):
        self.rows = [('playing', 'speech', 20), ('next', 'speech', 3)]
        await self.play_until(2)
        async with asyncio.timeout(1):
            async with self.publisher.cutting() as cut:
                self.assertTrue(cut)
                # 준비한 멘트는 없던 것이 돼서 호출자가 대기열에서 버릴 수 있다
                self.assertIsNone(self.publisher.journal.status('next'))
                self.assertIsNone(self.publisher.active_id)
                self.assertFalse(list(self.publisher.directory.glob('next_*.ts')))
                played = self.seen.count('playing')
                await asyncio.sleep(0.5)
                # 블록 안에서는 다음 조각을 고르지 않는다 — 채움만 나간다
                self.assertEqual(self.seen.count('playing'), played)
                self.assertNotIn('next', self.seen)
        self.assertLess(played, 6)  # 20조각 가운데 나간 데까지만
        self.assertEqual(self.publisher.journal.status('playing'), 'PUBLISHED')  # 나간 데까지는 방송된 것
        self.assertFalse(list(self.publisher.directory.glob('playing_1?.ts')))  # 남은 조각 파일은 지운다
        self.assertEqual(self.publisher.unpublished_seconds, 0)
        self.assertEqual(set(self.seen[played:]), {'fallback'})

    async def test_prepared_music_is_kept(self):
        self.rows = [('intro', 'speech', 20), ('song', 'music', 2)]
        await self.play_until(2)
        async with asyncio.timeout(1):
            async with self.publisher.cutting() as cut:
                self.assertTrue(cut)
                self.assertEqual(self.publisher.journal.status('song'), 'MIXING')
        async with asyncio.timeout(3):
            while self.seen.count('song') < 2:
                await asyncio.sleep(0.01)
        self.assertLess(self.seen.count('intro'), 6)

    async def test_release_drops_an_encoded_speech(self):
        self.rows = [("next", "speech", 3)]
        self.publisher.preparation = asyncio.create_task(self.publisher.wait_for_next())
        await self.publisher.preparation
        await self.publisher.release_prepared()
        self.assertIsNone(self.publisher.journal.status("next"))
        self.assertIsNone(self.publisher.preparation)
        self.assertEqual(self.publisher.unpublished_seconds, 0)
        self.assertFalse(list(self.publisher.directory.glob("next_*.ts")))

    async def test_release_cancels_a_speech_being_encoded_but_not_a_song(self):
        for kind, status in (("speech", None), ("music", "MIXING")):
            with self.subTest(kind=kind):
                self.rows = [("slow_" + kind, kind, 3)]
                self.encoding = asyncio.Event()
                task = self.publisher.preparation = asyncio.create_task(self.publisher.wait_for_next())
                await asyncio.sleep(0.05)
                await self.publisher.release_prepared()
                self.assertEqual(self.publisher.journal.status("slow_" + kind), status)
                self.assertEqual(task.cancelled(), kind == "speech")
                self.encoding.set()
                if kind == "music":
                    self.assertEqual((await task)[0]["id"], "slow_music")

    async def test_cutting_without_a_running_publisher_does_nothing(self):
        self.publisher.task = asyncio.create_task(asyncio.sleep(0))
        await self.publisher.task
        async with asyncio.timeout(1):
            async with self.publisher.cutting() as cut:
                self.assertFalse(cut)
                self.assertEqual(self.publisher.holds, 1)
        self.assertEqual(self.publisher.holds, 0)


class ForgetTests(unittest.TestCase):
    def test_forget_removes_only_an_unpublished_job(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / 'journal.sqlite', 'st_test')
            try:
                journal.transition({**segment(), 'id': 'mixing'}, 'MIXING')
                journal.transition({**segment(), 'id': 'published'}, 'PUBLISHED')
                journal.forget('mixing')
                journal.forget('published')
                journal.forget('unknown')
                self.assertIsNone(journal.status('mixing'))
                self.assertEqual(journal.status('published'), 'PUBLISHED')
                self.assertEqual([row['id'] for row in journal.recover()], ['published'])
            finally:
                journal.close()


class PrepareHoldTests(unittest.IsolatedAsyncioTestCase):
    async def test_publisher_selects_nothing_while_held(self):
        publisher = HlsPublisher(None, Settings('127.0.0.1', 3000))  # Redis를 건드리면 실패한다
        publisher.holds = 1
        self.assertIsNone(await publisher.prepare_next())


class TestBroadcastOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.station = 'st_cut_' + uuid.uuid4().hex
        self.prefix = f'backend:{self.station}'
        self.redis = Redis.from_url('redis://127.0.0.1:6379/15', decode_responses=True)
        self.app = create_app(Settings('127.0.0.1', 3000, station_id=self.station, hls_enabled=True))
        self.app.state.redis = self.redis
        self.hls = fake_publisher(current='playing', active_id='prepared')
        self.cuts = 0

        @contextlib.asynccontextmanager
        async def cutting():
            self.cuts += 1
            self.hls.active_id = None  # 준비한 멘트를 놓아 준다
            yield True

        self.hls.cutting = cutting
        default_runtime(self.app).hls = self.hls
        self.app.dependency_overrides[get_current_user] = lambda: {'id': 'a1', 'kind': 'admin'}
        ids = ['playing', 'prepared', 'speech1']
        await self.redis.rpush(self.prefix + ':queue', *ids)
        await self.redis.hset(self.prefix + ':segments', mapping={key: queued(key) for key in ids})
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://test')
        self.path = f'/api/stations/{self.station}/corner-skip'

    async def asyncTearDown(self):
        await self.client.aclose()
        keys = [k async for k in self.redis.scan_iter(match=f'{self.prefix}:*')]
        keys += [k async for k in self.redis.scan_iter(match=f'engine:{self.station}:*')]
        if keys:
            await self.redis.delete(*keys)
        await self.redis.aclose()

    async def discarded(self):
        events = [(f['type'], json.loads(f['payload'])) for _, f in await self.redis.xrange(f'engine:{self.station}:in')]
        return next(payload['segment_ids'] for kind, payload in events if kind == 'segment.discarded')

    async def test_test_broadcast_cuts_and_discards_the_prepared_speech(self):
        await self.redis.set(self.prefix + ':station', json.dumps({'status': 'started', 'test': True}))
        response = await self.client.post(self.path, json={'corner': 'text_corner'})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.cuts, 1)
        self.assertEqual(await self.discarded(), ['prepared', 'speech1'])
        self.assertEqual(await self.redis.lrange(self.prefix + ':queue', 0, -1), ['playing'])

    async def test_real_broadcast_keeps_playing(self):
        await self.redis.set(self.prefix + ':station', json.dumps({'status': 'started'}))
        response = await self.client.post(self.path, json={'corner': 'text_corner'})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.cuts, 0)
        self.assertEqual(await self.discarded(), ['speech1'])
