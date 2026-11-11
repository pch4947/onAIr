"""Length of the silence chunk that fills the stream while the next segment is not ready."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings, load_settings
from app.streaming.hls import HlsPublisher


class FillChunkSettingTests(unittest.TestCase):
    def test_default_keeps_two_seconds_and_range_is_checked(self):
        with patch('app.config.load_dotenv'), patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ONAIR_FILL_CHUNK_SECONDS', None)
            self.assertEqual(load_settings().fill_chunk_seconds, 2)
        for raw, expected in (('0.5', 0.5), ('1', 1.0)):
            with patch('app.config.load_dotenv'), patch.dict(os.environ, {'ONAIR_FILL_CHUNK_SECONDS': raw}):
                self.assertEqual(load_settings().fill_chunk_seconds, expected)
        for raw in ('0.1', '5', 'nan', 'abc'):
            with self.subTest(raw=raw), patch('app.config.load_dotenv'), \
                    patch.dict(os.environ, {'ONAIR_FILL_CHUNK_SECONDS': raw}), self.assertRaises(ValueError):
                load_settings()


@unittest.skipUnless(os.environ.get('ONAIR_FFMPEG'), 'Requires FFmpeg')
class FillChunkEncodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_silence_chunk_has_the_configured_length(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for seconds in (0.5, 2):
                with self.subTest(seconds=seconds):
                    publisher = HlsPublisher(None, Settings('127.0.0.1', 3000, hls_dir=root, ffmpeg=os.environ['ONAIR_FFMPEG'],
                                                            fill_chunk_seconds=seconds))
                    publisher.directory.mkdir(parents=True, exist_ok=True)
                    chunks = await publisher.encode(None)
                    self.assertEqual(len(chunks), 1)
                    self.assertAlmostEqual(chunks[0][1], seconds, delta=0.05)


if __name__ == '__main__':
    unittest.main()
