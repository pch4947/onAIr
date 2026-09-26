import asyncio
import os
from pathlib import Path
import tempfile
import unittest

from app.config import Settings
from app.streaming.hls import HlsPublisher


@unittest.skipUnless(os.environ.get("ONAIR_FFMPEG"), "Set ONAIR_FFMPEG for real audio encoding tests")
class HlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_wav_mp3_playlist_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings("127.0.0.1", 3000, hls_dir=root,
                                ffmpeg=os.environ["ONAIR_FFMPEG"])
            publisher = HlsPublisher(None, settings)
            publisher.directory.mkdir()
            for extension in ["wav", "mp3"]:
                source = root / f"source.{extension}"
                process = await asyncio.create_subprocess_exec(
                    settings.ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=44100", "-t", "9", str(source))
                self.assertEqual(await process.wait(), 0)
                chunks = await publisher.encode(source)
                self.assertGreaterEqual(len(chunks), 3)
                self.assertAlmostEqual(sum(duration for _, duration in chunks), 9, delta=0.2)
                for index, (name, duration) in enumerate(chunks):
                    publisher.publish(name, duration, index == 0)
            playlist = (publisher.directory / "index.m3u8").read_text()
            self.assertIn("#EXT-X-DISCONTINUITY", playlist)
            self.assertNotIn("#EXT-X-ENDLIST", playlist)
            self.assertTrue(publisher.ready)
            self.assertFalse((publisher.directory / "index.m3u8.tmp").exists())
            # Decode the mixed-source HLS playlist with FFmpeg, not just file existence.
            process = await asyncio.create_subprocess_exec(
                settings.ffmpeg, "-loglevel", "error", "-i", str(publisher.directory / "index.m3u8"),
                "-t", "8", "-f", "null", "-", stdout=asyncio.subprocess.DEVNULL)
            async with asyncio.timeout(20):
                self.assertEqual(await process.wait(), 0)
            initial_sequence = publisher.sequence
            for _ in range(5):
                chunks = await publisher.encode(None)
                for index, (name, duration) in enumerate(chunks):
                    publisher.publish(name, duration, index == 0)
            self.assertGreater(publisher.sequence, initial_sequence)
            self.assertGreater(publisher.discontinuities, 0)
            self.assertGreaterEqual(sum(item[1] for item in publisher.entries), 24)
            self.assertTrue(all((publisher.directory / name).is_file() for name, _, _ in publisher.entries))
            invalid = root / "invalid.wav"
            invalid.write_bytes(b"not audio")
            with self.assertRaises(ValueError):
                await publisher.encode(invalid)
