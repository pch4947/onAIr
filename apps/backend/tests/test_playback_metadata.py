"""Client playback metadata must not follow the publisher's newer source."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from fastapi import Response

from app.api import BroadcastState, stream_state
from app.config import Settings
from app.streaming.hls import HlsPublisher


class PlaybackMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_listener_silence_expiry_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings('127.0.0.1', 3000, hls_enabled=True, hls_dir=Path(directory))
            publisher = HlsPublisher(None, settings)
            publisher.directory.mkdir()
            first = 'a' * 32 + '_' + 'b' * 32 + '_0.ts'
            second = 'a' * 32 + '_' + 'c' * 32 + '_0.ts'
            publisher.current = dict(id='opening', corner_type='opening', kind='speech',
                                     duration_ms=8000, audio_ref='private/path.wav', request_ref='private-request')
            self.assertEqual(publisher.playback_metadata(first)['status'], 'unavailable')
            publisher.publish(first, 4, True)
            publisher.current = dict(id='next', corner_type='briefing', kind='speech', duration_ms=9000)
            publisher.publish(second, 4, True)
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
                broadcast=BroadcastState(), hls=publisher, settings=settings,
                requests=SimpleNamespace(metrics=AsyncMock(return_value={})))))
            response = Response()
            result = await stream_state(request, response, first)
            self.assertEqual(result['stream']['currentSegment']['id'], 'opening')
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertNotIn('audio_ref', result['stream']['currentSegment'])
            self.assertNotIn('request_ref', result['stream']['currentSegment'])
            self.assertIsNone((await stream_state(request, Response()))['stream']['currentSegment'])
            self.assertIsNone(request.app.state.broadcast.stream['currentSegment'])
            publisher.current = None
            with patch('app.streaming.hls.time.monotonic', return_value=100):
                for index in range(8):
                    publisher.publish(f'silence_{index}.ts', 4, True)
            # First source is outside the playlist but still valid for a buffered listener.
            self.assertNotIn(first, [entry[0] for entry in publisher.entries])
            self.assertEqual(publisher.playback_metadata(first)['segment']['id'], 'opening')
            silence = await stream_state(request, Response(), 'silence_7.ts')
            self.assertEqual(silence['stream']['playback']['status'], 'silence')
            self.assertIsNone(silence['stream']['currentSegment'])
            with patch('app.streaming.hls.time.monotonic', return_value=161):
                publisher.publish('later.ts', 4, True)
            self.assertEqual(publisher.playback_metadata(first)['status'], 'unavailable')
            # A new publisher cannot claim an old browser fragment belongs to its current source.
            restarted = HlsPublisher(None, settings)
            self.assertEqual(restarted.playback_metadata(second)['status'], 'unavailable')
