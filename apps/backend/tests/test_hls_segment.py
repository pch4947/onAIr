"""HLS 조각 길이 설정과 재생목록 TARGETDURATION (#359)."""
import asyncio
import math
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import Settings, load_settings
from app.streaming.hls import HlsPublisher, segment_seconds, target_duration


def settings(root, **overrides):
    return Settings('127.0.0.1', 3000, hls_dir=root, ffmpeg=os.environ.get('ONAIR_FFMPEG', 'ffmpeg'), **overrides)


class TargetDurationTests(unittest.TestCase):
    def test_target_covers_segment_and_fill_chunks(self):
        cases = [((2, 2), 2), ((4, 2), 4), ((1.5, 2), 2), ((2.5, 2), 3), ((1, 0.25), 1), ((2, 4), 4)]
        for (segment, fill), expected in cases:
            with self.subTest(segment=segment, fill=fill):
                self.assertEqual(target_duration(SimpleNamespace(hls_segment_seconds=segment, fill_chunk_seconds=fill)), expected)

    def test_fake_settings_without_the_field_use_the_default(self):
        self.assertEqual(segment_seconds(SimpleNamespace()), Settings.hls_segment_seconds)


class ConfigTests(unittest.TestCase):
    def test_default_and_range(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ONAIR_HLS_SEGMENT_SECONDS', None)
            self.assertEqual(load_settings().hls_segment_seconds, 2)
        with patch.dict(os.environ, {'ONAIR_HLS_SEGMENT_SECONDS': '3'}):
            self.assertEqual(load_settings().hls_segment_seconds, 3)
        for bad in ('0.5', '7', 'nan'):
            with self.subTest(bad=bad), patch.dict(os.environ, {'ONAIR_HLS_SEGMENT_SECONDS': bad}):
                with self.assertRaises(ValueError):
                    load_settings()


class PlaylistTests(unittest.TestCase):
    def test_live_and_vod_playlists_use_the_configured_target(self):
        with tempfile.TemporaryDirectory() as directory:
            for segment, expected in ((2, 2), (4, 4)):
                with self.subTest(segment=segment):
                    publisher = HlsPublisher(None, settings(Path(directory), hls_segment_seconds=segment))
                    publisher.directory.mkdir(exist_ok=True)
                    publisher.publish('a.ts', segment, True)
                    live = (publisher.directory / 'index.m3u8').read_text()
                    self.assertIn(f'#EXT-X-TARGETDURATION:{expected}\n', live)
                    publisher.vod_entries.append(('a.ts', segment, True))
                    (publisher.directory / 'vod' / publisher.session).mkdir(parents=True)
                    publisher.write_vod_manifest(final=True)
                    vod = (publisher.directory / 'vod' / publisher.session / 'index.m3u8').read_text()
                    self.assertIn(f'#EXT-X-TARGETDURATION:{expected}\n', vod)


@unittest.skipUnless(os.environ.get('ONAIR_FFMPEG'), 'Set ONAIR_FFMPEG for real audio encoding tests')
class EncodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_second_chunks_stay_within_the_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = settings(root, hls_segment_seconds=2)
            publisher = HlsPublisher(None, config)
            publisher.directory.mkdir()
            source = root / 'source.wav'
            process = await asyncio.create_subprocess_exec(
                config.ffmpeg, '-loglevel', 'error', '-f', 'lavfi', '-i',
                'sine=frequency=440:sample_rate=44100', '-t', '9.3', str(source))
            self.assertEqual(await process.wait(), 0)
            chunks = await publisher.encode(source)
            durations = [duration for _, duration in chunks]
            self.assertEqual(len(chunks), 5)  # 2+2+2+2+1.3
            self.assertAlmostEqual(sum(durations), 9.3, delta=0.2)
            # 반올림한 조각 길이가 TARGETDURATION을 넘지 않는다 (재생목록 규칙)
            self.assertTrue(all(round(d) <= target_duration(config) for d in durations), durations)
            fill = await publisher.encode(None)
            self.assertEqual(len(fill), 1)
            for index, (name, duration) in enumerate(chunks + fill):
                publisher.publish(name, duration, index in (0, len(chunks)))
            playlist = (publisher.directory / 'index.m3u8').read_text()
            listed = [float(value) for value in re.findall(r'#EXTINF:([0-9.]+),', playlist)]
            self.assertLessEqual(max(round(value) for value in listed), 2)
            self.assertIn('#EXT-X-TARGETDURATION:2\n', playlist)
            process = await asyncio.create_subprocess_exec(
                config.ffmpeg, '-loglevel', 'error', '-live_start_index', '0',
                '-i', str(publisher.directory / 'index.m3u8'), '-t', '9', '-f', 'null', '-', stdout=asyncio.subprocess.DEVNULL)
            async with asyncio.timeout(20):
                self.assertEqual(await process.wait(), 0)
            self.assertTrue(math.isfinite(sum(listed)))
