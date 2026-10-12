"""Glue is ordered narration, never listener-request completion."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from app.config import Settings
from app.messaging.contracts import Segment
from app.streaming.hls import HlsPublisher
from app.streaming.journal import Journal
from test_broadcast_runtime import segment


class GlueTests(unittest.IsolatedAsyncioTestCase):
    async def test_glue_does_not_jump_normal_speech(self):
        publisher = HlsPublisher(AsyncMock(), Settings('127.0.0.1', 3000))
        publisher.journal = Mock()
        publisher.journal.status.return_value = None
        normal = {**segment(), 'id': 'normal', 'corner_type': 'opening'}
        glue = {**segment('glue'), 'id': 'glue', 'priority': 1}
        self.assertEqual(Segment(**glue).kind, 'glue')
        pipe = AsyncMock()
        pipe.lrange = Mock()
        pipe.hgetall = Mock()
        pipe.__aenter__.return_value = pipe
        pipe.execute.return_value = (['normal', 'glue'], {
            'normal': json.dumps(normal), 'glue': json.dumps(glue)})
        publisher.redis.pipeline = Mock(return_value=pipe)
        self.assertEqual((await publisher.select_next()).id, 'normal')

    async def test_glue_receives_same_bgm_ducking_as_speech(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'st_test').mkdir()
            source = root / 'st_test/a.wav'
            source.write_bytes(b'shared glue asset')
            publisher = HlsPublisher(AsyncMock(), Settings('127.0.0.1', 3000,
                station_id='st_test', audio_dir=root, bgm_audio=root / 'bgm.wav'))
            publisher.journal = Journal(root / 'journal.sqlite', 'st_test')
            try:
                publisher.select_next = AsyncMock(return_value=Segment(**{
                    **segment('glue'), 'request_ref': None}))
                publisher.encode = AsyncMock(return_value=[('chunk.ts', 4)])
                await publisher.prepare_next()
                publisher.encode.assert_awaited_once_with(source, bgm_duration=8)
                self.assertTrue(source.exists())
            finally:
                publisher.journal.close()


@unittest.skipUnless(os.environ.get('ONAIR_FFMPEG'), 'Requires FFmpeg')
class BgmEncodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_looped_bed_has_finite_voice_duration_and_decodes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ffmpeg = os.environ['ONAIR_FFMPEG']
            for file, duration, frequency in [('voice.wav', 1, 440), ('bed.wav', .2, 220)]:
                subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                    f'sine=frequency={frequency}:sample_rate=48000', '-t', str(duration),
                    str(root / file)], check=True, timeout=10)
            publisher = HlsPublisher(None, Settings('127.0.0.1', 3000,
                hls_dir=root / 'hls', ffmpeg=ffmpeg, bgm_audio=root / 'bed.wav'))
            publisher.directory.mkdir(parents=True)
            chunks = await publisher.encode(root / 'voice.wav', bgm_duration=1)
            self.assertLess(sum(duration for _, duration in chunks), 1.2)
            self.assertGreater(sum(duration for _, duration in chunks), .9)
            for name, _ in chunks:
                subprocess.run([ffmpeg, '-v', 'error', '-i', str(publisher.directory / name),
                    '-f', 'null', '-'], check=True, timeout=10)
