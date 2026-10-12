"""Internal Lavalink boundary: reject invalid metadata and incomplete streams."""
import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import httpx

from app.youtube_worker import download, DownloadFailure, LavalinkClient

VIDEO = 'jNQXAC9IVRw'


class DownloadTests(unittest.TestCase):
    def test_real_client_reads_to_eof_without_unbounded_read(self):
        client = LavalinkClient('http://internal', 'test-password')
        response = io.BytesIO(b'complete-media')
        response.code = 200
        with patch.object(client.opener, 'open', return_value=response):
            with client.stream('GET', '/youtube/stream/' + VIDEO, {'withClient': 'TVHTML5'}) as stream:
                self.assertEqual(b''.join(stream.iter_bytes(4)), b'complete-media')

    def test_actual_long_file_is_rejected_when_metadata_is_blocked(self):
        def handler(request):
            return (httpx.Response(200, json={'loadType': 'empty'}) if request.url.path == '/v4/loadtracks'
                    else httpx.Response(200, content=b'full-media'))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'ONAIR_LAVALINK_PASSWORD': 'test'}), \
                patch('subprocess.run', return_value=SimpleNamespace(stdout=json.dumps({
                    'format': {'duration': '601'}, 'streams': [{'codec_type': 'audio'}]}))):
            with httpx.Client(transport=httpx.MockTransport(handler), base_url='http://internal') as client:
                with self.assertRaises(ValueError):
                    download(VIDEO, directory, 600, client=client)
            self.assertFalse((Path(directory) / 'result.json').exists())

    def run_download(self, handler, limit=600):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'ONAIR_LAVALINK_PASSWORD': 'internal-test-password'}):
            measured = json.dumps({'format': {'duration': '19'}, 'streams': [{'codec_type': 'audio'}]})
            with httpx.Client(transport=httpx.MockTransport(handler), base_url='http://internal') as client, \
                    patch('subprocess.run', return_value=SimpleNamespace(stdout=measured)):
                download(VIDEO, directory, limit, client=client)
            root = Path(directory)
            return json.loads((root / 'result.json').read_text()), (root / 'source.media').read_bytes()

    def metadata(self, **overrides):
        return {'loadType': 'track', 'data': {'info': dict(identifier=VIDEO, length=19000,
            isStream=False, title='test', author='artist') | overrides}}

    def test_complete_stream_ignores_sentinel_content_length(self):
        def handler(request):
            if request.url.path == '/v4/loadtracks':
                return httpx.Response(200, json=self.metadata())
            self.assertEqual(request.url.path, '/youtube/stream/' + VIDEO)
            self.assertEqual(request.url.params['withClient'], 'TVHTML5')
            return httpx.Response(200, headers={'Content-Length': str(2**63 - 1)}, content=b'full-media')
        metadata, data = self.run_download(handler)
        self.assertEqual(metadata['duration_ms'], 19000)
        self.assertEqual(data, b'full-media')

    def test_blocked_metadata_uses_id_and_measured_duration(self):
        def handler(request):
            return (httpx.Response(200, json={'loadType': 'empty'}) if request.url.path == '/v4/loadtracks'
                    else httpx.Response(200, content=b'full-media'))
        metadata, _ = self.run_download(handler)
        self.assertEqual(metadata['title'], VIDEO)
        self.assertEqual(metadata['duration_ms'], 19000)

    def test_actual_byte_limit_ignores_upstream_headers(self):
        def handler(request):
            return (httpx.Response(200, json=self.metadata()) if request.url.path == '/v4/loadtracks'
                    else httpx.Response(200, content=b'too-large'))
        with patch('app.youtube_worker.MAX_DOWNLOAD_BYTES', 4), \
                self.assertRaisesRegex(DownloadFailure, 'size_limit'):
            self.run_download(handler)

    def test_rejects_long_live_or_wrong_video_before_download(self):
        for info in [dict(length=601000), dict(isStream=True), dict(identifier='other')]:
            calls = []
            def handler(request):
                calls.append(request.url.path)
                return httpx.Response(200, json=self.metadata(**info))
            with self.subTest(info=info), self.assertRaises(ValueError):
                self.run_download(handler)
            self.assertEqual(calls, ['/v4/loadtracks'])

    def test_service_auth_and_stream_failure_have_fixed_codes(self):
        for code, expected in [(401, 'service_auth'), (503, 'service_unavailable')]:
            with self.subTest(code=code), self.assertRaisesRegex(DownloadFailure, expected):
                self.run_download(lambda request: httpx.Response(code, text='private upstream details'))
        def handler(request):
            return (httpx.Response(200, json=self.metadata()) if request.url.path == '/v4/loadtracks'
                    else httpx.Response(500, text='private OAuth details'))
        with self.assertRaisesRegex(DownloadFailure, 'stream_failed_check_lavalink'):
            self.run_download(handler)

    def test_empty_or_interrupted_stream_has_no_result_manifest(self):
        class Interrupted(httpx.SyncByteStream):
            def __iter__(self):
                yield b'partial'
                raise httpx.ReadError('connection lost')
        for stream in [httpx.ByteStream(b''), Interrupted()]:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'ONAIR_LAVALINK_PASSWORD': 'test'}):
                def handler(request):
                    return (httpx.Response(200, json=self.metadata()) if request.url.path == '/v4/loadtracks'
                            else httpx.Response(200, stream=stream))
                with httpx.Client(transport=httpx.MockTransport(handler), base_url='http://internal') as client:
                    with self.assertRaises((DownloadFailure, httpx.ReadError)):
                        download(VIDEO, directory, 600, client=client)
                self.assertFalse((Path(directory) / 'result.json').exists())
