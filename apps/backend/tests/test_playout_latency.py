"""Priority barriers, bounded prefetch, and reusable fallback regression tests."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from app.config import Settings
from app.streaming.hls import HlsPublisher
from app.streaming.journal import Journal
from test_broadcast_runtime import segment


class PlayoutLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefetched_audio_not_counted_again_in_queue(self):
        publisher = HlsPublisher(None, Settings('127.0.0.1', 3000))
        publisher.current = {'id': 'playing'}
        publisher.active_id = 'prepared'
        publisher.unpublished_seconds = 12
        publisher.queued = {'playing': 8, 'prepared': 8, 'waiting': 7}
        publisher.queue_available = True
        self.assertEqual(publisher.buffer_snapshot()['totalAudioEstimateSeconds'], 19)

    async def test_priority_stability_and_fixed_barrier(self):
        publisher = HlsPublisher(AsyncMock(), Settings('127.0.0.1', 3000))
        publisher.journal = Mock()
        publisher.journal.status.return_value = None
        pipe = AsyncMock()
        pipe.lrange = Mock()
        pipe.hgetall = Mock()
        pipe.__aenter__.return_value = pipe
        publisher.redis.pipeline = Mock(return_value=pipe)

        async def select(rows):
            pipe.execute.return_value = ([r['id'] for r in rows], {r['id']: json.dumps(r) for r in rows})
            return (await publisher.select_next()).id

        normal = {**segment(), 'id': 'normal', 'corner_type': 'talk'}
        reply = {**segment(), 'id': 'reply', 'priority': 30}
        ack = {**segment('ack'), 'id': 'ack', 'priority': 10, 'reorderable': False}
        self.assertEqual(await select([normal, reply, ack]), 'ack')
        self.assertEqual(await select([normal, reply]), 'reply')
        self.assertEqual(await select([normal, {**normal, 'id': 'second'}]), 'normal')
        self.assertEqual(await select([{**normal, 'reorderable': False}, ack]), 'normal')
        publisher.current = normal
        self.assertEqual(await select([normal, reply]), 'reply')

    async def test_fallback_encoded_once_with_unique_files(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = HlsPublisher(None, Settings('127.0.0.1', 3000, hls_dir=Path(directory)))
            publisher.directory.mkdir()
            (publisher.directory / 'template.ts').write_bytes(b'audio')
            publisher.encode = AsyncMock(return_value=[('template.ts', 4)])
            first = await publisher.fallback_chunks()
            second = await publisher.fallback_chunks()
            publisher.encode.assert_awaited_once()
            self.assertNotEqual(first[0][0], second[0][0])
            for name, _ in first + second:
                self.assertEqual((publisher.directory / name).read_bytes(), b'audio')

    async def test_next_encode_overlaps_current_without_intermediate_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            publisher = HlsPublisher(None, Settings('127.0.0.1', 3000))
            publisher.journal = Journal(Path(directory) / 'journal.sqlite', 'st_test')
            seen = []
            began_during_current = []
            done = asyncio.Event()
            calls = 0

            async def prepare():
                nonlocal calls
                calls += 1
                if calls > 2:
                    return None
                number = calls
                if number == 2:
                    began_during_current.append(publisher.current['id'])
                    await asyncio.sleep(0.12)
                row = {**segment(), 'id': f'seg_{number}'}
                publisher.unpublished_seconds += 0.4
                publisher.active_id = row['id']
                return row, [(f'{number}a', 0.2), (f'{number}b', 0.2)]

            def publish(name, duration, boundary):
                seen.append(publisher.current['id'] if publisher.current else 'fallback')
                publisher.entries.append((name, duration, boundary))
                if len(seen) == 4:
                    done.set()

            publisher.prepare_next = prepare
            publisher.publish = publish
            publisher.fallback_chunks = AsyncMock(return_value=[('fallback', 0.1)])
            publisher.task = asyncio.create_task(publisher.run())
            try:
                await asyncio.wait_for(done.wait(), 3)
                self.assertEqual(seen, ['seg_1', 'seg_1', 'seg_2', 'seg_2'])
                self.assertEqual(began_during_current, ['seg_1'])
                publisher.fallback_chunks.assert_not_awaited()
                self.assertAlmostEqual(publisher.unpublished_seconds, 0)
            finally:
                await publisher.stop()
