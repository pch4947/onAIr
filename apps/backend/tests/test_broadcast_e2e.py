"""Real engine transport -> Redis -> backend -> HTTP HLS, using local test tones."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler
import uuid

from redis import Redis


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1'
                     and os.environ.get('ONAIR_FFMPEG'), 'Requires local Redis, engine and FFmpeg')
class BroadcastE2ETests(unittest.TestCase):
    def test_fifo_http_late_join_and_empty_queue(self):
        from onair_engine.domain import SegmentSubmission, SegmentKind
        from onair_engine.transport import RedisTransport

        station = 'st_e2e_' + uuid.uuid4().hex
        redis_url = 'redis://127.0.0.1:6379/0'
        redis = Redis.from_url(redis_url, decode_responses=True)
        self.addCleanup(redis.close)
        redis.ping()
        def clean_keys():
            for pattern in [f'engine:{station}:*', f'backend:{station}:*']:
                keys = list(redis.scan_iter(match=pattern))
                if keys:
                    redis.delete(*keys)
        self.addCleanup(clean_keys)
        ffmpeg = os.environ['ONAIR_FFMPEG']
        client = build_opener(ProxyHandler({}))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / station).mkdir()
            submissions = []
            for index, extension in enumerate(['wav', 'mp3']):
                audio_ref = f'{station}/source{index}.{extension}'
                subprocess.run([ffmpeg, '-loglevel', 'error', '-f', 'lavfi', '-i',
                    f'sine=frequency={330 + index * 110}:sample_rate=44100',
                    '-t', '9', str(root / audio_ref)], check=True, timeout=15,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                submissions.append(SegmentSubmission(id=f'seg_{index}', station_id=station,
                    audio_ref=audio_ref, duration_ms=9000, corner_type='opening' if index == 0 else 'briefing',
                    kind=SegmentKind.SPEECH, priority=50, reorderable=True,
                    request_ref=None, music_ref=None, created_at=1.0))

            async def send():
                transport = RedisTransport(redis_url, station)
                try:
                    for submission in submissions:
                        await transport.publish_segment(submission)
                finally:
                    await transport.close()
            asyncio.run(send())
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            base = f'http://127.0.0.1:{port}'
            def get(path):
                with client.open(base + path, timeout=3) as response:
                    return response.read()
            server = subprocess.Popen([sys.executable, '-m', 'app'],
                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=dict(os.environ, HOST='127.0.0.1', PORT=str(port),
                    ONAIR_REDIS_URL=redis_url, ONAIR_STATION_ID=station, ONAIR_FALLBACK_MODE='silence',
                    ONAIR_AUDIO_DIR=str(root), ONAIR_HLS_DIR=str(root / 'hls'), ONAIR_HLS_ENABLED='1'))
            try:
                deadline = time.monotonic() + 45
                seen = []
                sequences = []
                checked = set()
                decoded = False
                silence_after_sources = False
                while time.monotonic() < deadline:
                    self.assertIsNone(server.poll(), 'Backend exited during broadcast')
                    try:
                        playlist = get(f'/hls/{station}/index.m3u8').decode()
                    except (HTTPError, URLError):
                        time.sleep(0.2)
                        continue
                    self.assertNotIn('#EXT-X-ENDLIST', playlist)
                    sequences.append(int(next(line.split(':')[1] for line in playlist.splitlines()
                                              if line.startswith('#EXT-X-MEDIA-SEQUENCE:'))))
                    for name in playlist.splitlines():
                        if not name or name.startswith('#') or name in checked:
                            continue
                        checked.add(name)
                        self.assertGreater(len(get(f'/hls/{station}/{name}')), 0)
                        metadata = json.loads(get('/api/stream/state?fragment=' + name))['stream']['playback']
                        self.assertNotEqual(metadata['status'], 'unavailable')
                        if metadata['segment']:
                            segment_id = metadata['segment']['id']
                            if not seen or seen[-1] != segment_id:
                                seen.append(segment_id)
                        elif seen == ['seg_0', 'seg_1']:
                            silence_after_sources = True
                    if not decoded:
                        # Join after the live playlist is ready and decode actual HTTP audio.
                        pcm = subprocess.run([ffmpeg, '-loglevel', 'error', '-i',
                            base + f'/hls/{station}/index.m3u8', '-t', '4', '-ac', '1',
                            '-ar', '8000', '-f', 's16le', '-'], check=True, timeout=20,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
                        self.assertGreater(len(pcm), 32000)
                        self.assertTrue(any(pcm), 'HTTP stream contained only silence')
                        decoded = True
                    if silence_after_sources and sequences[-1] > sequences[0]:
                        break
                    time.sleep(0.2)
                self.assertEqual(seen, ['seg_0', 'seg_1'], 'Source order changed or a source was lost')
                self.assertTrue(decoded, 'HTTP late-join decode did not complete')
                self.assertTrue(silence_after_sources, 'Empty queue did not produce silence')
                self.assertGreater(sequences[-1], sequences[0], 'Live window did not advance')
                self.assertEqual(sequences, sorted(sequences))
                self.assertEqual(redis.llen(f'backend:{station}:queue'), 0)
                self.assertEqual(redis.xpending(f'engine:{station}:out', 'backend')['pending'], 0)
                self.assertEqual(json.loads(get('/health/redis'))['ok'], True)
            finally:
                server.terminate()
                server.wait(timeout=10)
