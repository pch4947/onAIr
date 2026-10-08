"""Broadcast clock, durable recovery, and non-blocking playout under Redis failure."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from redis.exceptions import ConnectionError

from app.config import Settings
from app.streaming.hls import HlsPublisher
from app.streaming.journal import Journal
from app.streaming.ownership import StationLock


def segment(kind='speech'):
    return dict(id='seg_one', station_id='st_test', audio_ref='st_test/a.wav',
                duration_ms=8000, corner_type='request_reply', kind=kind, priority=50,
                reorderable=True, request_ref='req_one', music_ref=None, created_at=1.0)


class JournalTests(unittest.IsolatedAsyncioTestCase):
    async def test_outbox_survives_failure_and_restart_ack_is_not_request_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'journal.sqlite'
            journal = Journal(path, 'st_test')
            journal.transition(segment(), 'MIXING')
            journal.transition(segment(), 'PUBLISHED')
            journal.transition({**segment('ack'), 'id': 'ack_one'}, 'PLAYED')
            failing = AsyncMock()
            failing.eval.side_effect = ConnectionError('offline')
            with self.assertRaises(ConnectionError):
                await journal.flush(failing)
            self.assertEqual(journal.pending(), 3)
            journal.close()
            journal = Journal(path, 'st_test')
            self.assertEqual([item['id'] for item in journal.recover()], ['seg_one'])
            self.assertEqual(journal.status('ack_one'), 'PLAYED')
            recovered = AsyncMock()
            await journal.flush(recovered)
            self.assertEqual(journal.pending(), 0)
            # ack lifecycle must never update the parent request's state.
            self.assertEqual(recovered.eval.call_args_list[-1].args[-1], '')
            self.assertEqual(recovered.eval.call_args_list[0].args[-1], 'req_one')
            journal.close()

    async def test_single_publisher_directory_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'publisher.lock'
            first = StationLock(path)
            try:
                with self.assertRaises(RuntimeError):
                    StationLock(path)
            finally:
                first.close()
            StationLock(path).close()


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_survives_redis_outage_then_encoding_timeout_is_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = AsyncMock()
            client.hget.side_effect = ConnectionError('offline')
            publisher = HlsPublisher(client, Settings('127.0.0.1', 3000, audio_dir=root))
            publisher.journal = Journal(root / 'journal.sqlite', 'st_test')
            publisher.recovery.append(segment())
            try:
                self.assertIsNone(await publisher.prepare_next())
                self.assertEqual(len(publisher.recovery), 1)
                client.hget.side_effect = None
                client.hget.return_value = None
                publisher.encode = AsyncMock(side_effect=TimeoutError())
                with patch('app.streaming.hls.validate_audio', return_value=root / 'a.wav'):
                    with self.assertLogs('app.streaming.hls', level='WARNING'):
                        self.assertIsNone(await publisher.prepare_next())
                self.assertEqual(publisher.journal.status('seg_one'), 'FAILED')
                self.assertEqual(len(publisher.recovery), 0)
            finally:
                publisher.journal.close()

    async def test_buffer_excludes_fallback_and_does_not_count_active_twice(self):
        publisher = HlsPublisher(None, Settings('127.0.0.1', 3000))
        now = time.monotonic()
        publisher.timeline.extend([(now + 1, now + 5, True), (now + 5, now + 9, False)])
        publisher.unpublished_seconds = 3
        publisher.queued = {'active': 8, 'next': 12}
        publisher.active_id = 'active'
        publisher.queue_available = True
        snapshot = publisher.buffer_snapshot()
        self.assertAlmostEqual(snapshot['bufferSeconds'], 7, delta=0.1)
        self.assertEqual(snapshot['queuedAudioSeconds'], 12)
        self.assertAlmostEqual(snapshot['totalAudioEstimateSeconds'], 19, delta=0.1)
        publisher.queue_available = False
        self.assertIsNone(publisher.buffer_snapshot()['totalAudioEstimateSeconds'])

    async def test_playout_continues_offline_and_server_clock_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            client = AsyncMock()
            client.lindex.side_effect = ConnectionError('offline')
            client.eval.side_effect = ConnectionError('offline')
            client.pipeline = Mock(side_effect=ConnectionError('offline'))
            settings = Settings('127.0.0.1', 3000, station_id='st_test', ffmpeg=sys.executable,
                                hls_dir=Path(directory), broadcast_delay_seconds=0.1)
            publisher = HlsPublisher(client, settings)
            async def encode(_):
                name = f'{publisher.session}_{uuid.uuid4().hex}_0.ts'
                (publisher.directory / name).write_bytes(b'test-fragment')
                return [(name, 0.04)]
            publisher.encode = encode
            await publisher.start()
            try:
                publisher.finishing.append((time.monotonic() + 0.05, segment()))
                await asyncio.sleep(0.8)
                self.assertFalse(publisher.task.done())
                self.assertGreater(len(publisher.entries), 2)
                self.assertTrue(publisher.fallback_active)
                self.assertEqual(publisher.journal.status('seg_one'), 'PLAYED')
                self.assertGreater(publisher.journal.pending(), 0)
                self.assertFalse(publisher.queue_available)
                latest = publisher.entries[-1][0]
                self.assertEqual(publisher.playback_metadata(latest)['status'], 'fallback')
            finally:
                await publisher.stop()

    async def test_bad_source_does_not_block_later_queue_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = AsyncMock()
            client.lindex.return_value = 'seg_one'
            client.hget.side_effect = [json.dumps(segment()), None]
            settings = Settings('127.0.0.1', 3000, station_id='st_test', audio_dir=root)
            publisher = HlsPublisher(client, settings)
            publisher.journal = Journal(root / 'journal.sqlite', 'st_test')
            try:
                with self.assertLogs('app.streaming.hls', level='WARNING'):
                    self.assertIsNone(await publisher.prepare_next())
                self.assertEqual(publisher.journal.status('seg_one'), 'FAILED')
                self.assertIsNone(await publisher.prepare_next())
                client.eval.assert_awaited_once()
            finally:
                publisher.journal.close()
