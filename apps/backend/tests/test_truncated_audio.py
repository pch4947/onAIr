"""뒤가 잘린 음원 (#339): 받은 파일의 머리에 적힌 길이가 아니라 변환한 MP3를 실제로 잰 길이로 판단하고 알린다."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import music
from app.config import Settings
from app.music import MusicService, audio_seconds, ffprobe_path, run_process

VIDEO = 'BaW_jenozKc'


@unittest.skipUnless(os.environ.get('ONAIR_FFMPEG'), 'Requires FFmpeg')
class TruncatedAudioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = Settings('127.0.0.1', 3000, audio_dir=Path(temp.name), ffmpeg=os.environ['ONAIR_FFMPEG'],
                                 lavalink_password='test')
        self.service = MusicService(SimpleNamespace(), self.settings)
        self.directory = self.settings.audio_dir / self.settings.station_id / 'youtube'

    def downloading(self, actual_seconds, declared_seconds):
        """실제로는 actual_seconds만 들어 있는데 머리에는 declared_seconds로 적힌 파일을 받은 것처럼 한다."""
        async def worker(args, timeout, **kwargs):
            if args[0] != sys.executable:
                return await run_process(args, timeout)
            root = Path(args[4])
            await run_process([self.settings.ffmpeg, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                               'sine=frequency=440:sample_rate=48000', '-t', str(actual_seconds),
                               str(root / 'source.wav')], 15)
            (root / 'result.json').write_text(json.dumps(dict(
                source='source.wav', track_ref='yt:' + VIDEO, title='노래', artist='가수',
                duration_ms=round(declared_seconds * 1000))), encoding='utf-8')
        return patch('app.music.run_process', side_effect=worker)

    async def test_cut_short_download_is_rejected_and_nothing_is_cached(self):
        with self.downloading(2, 30), self.assertLogs(music.logger, 'WARNING') as logs, \
                self.assertRaises(HTTPException) as raised:
            await self.service.prepare(VIDEO, 'music_cut')
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(raised.exception.detail['code'], 'audio_truncated')
        self.assertIn('got 2.0s of 30.0s', logs.output[0])
        self.assertIn(VIDEO, logs.output[0])
        # 캐시에 남으면 같은 요청을 다시 보냈을 때 잘린 곡이 나간다
        self.assertFalse(self.service.is_cached('music_cut'))
        self.assertEqual(list(self.directory.glob('music_cut*')), [])

    async def test_whole_download_reports_the_length_that_will_actually_play(self):
        # 머리에 적힌 길이와 조금 달라도(허용 5초) 받아들이고, 길이는 MP3를 잰 값으로 알린다
        with self.downloading(2, 4):
            audio, metadata = await self.service.prepare(VIDEO, 'music_whole')
        self.assertAlmostEqual(metadata['duration_ms'], 2000, delta=150)
        self.assertAlmostEqual(await audio_seconds(ffprobe_path(self.settings), audio), 2.0, delta=0.15)
        self.assertEqual(json.loads((self.directory / 'music_whole.json').read_text(encoding='utf-8')), metadata)
        self.assertNotIn('source', metadata)

    async def test_tolerance_edge(self):
        with self.downloading(2, 6.5):  # 4.5초 모자람 — 받아들인다
            await self.service.prepare(VIDEO, 'music_near')
        with self.downloading(2, 8), self.assertLogs(music.logger, 'WARNING'), self.assertRaises(HTTPException):
            await self.service.prepare(VIDEO, 'music_far')  # 6초 모자람

    async def test_unreadable_audio_is_a_preparation_failure(self):
        broken = self.settings.audio_dir / 'broken.mp3'
        broken.write_bytes(b'not audio')
        with self.assertRaises(HTTPException) as raised:
            await audio_seconds(ffprobe_path(self.settings), broken)
        self.assertEqual(raised.exception.status_code, 502)
        with self.assertRaises(HTTPException) as missing:
            await audio_seconds(str(self.settings.audio_dir / 'no-such-ffprobe'), broken)
        self.assertEqual(missing.exception.status_code, 503)
