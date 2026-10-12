"""Isolated, bounded YouTube download; invoked by the backend, never with shell=True."""
import json
import math
from pathlib import Path
import sys

MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024

def validate_metadata(info, video_id, max_seconds):
    duration = info.get('duration')
    if (info.get('id') != video_id or info.get('_type', 'video') != 'video'
            or info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming', 'post_live')
            or info.get('age_limit', 0) > 0
            or not isinstance(duration, (int, float)) or not math.isfinite(duration)
            or not 0 < duration <= max_seconds):
        raise ValueError('Unsupported video: only finite, unrestricted videos within the duration limit')
    return {'track_ref': 'yt:' + video_id, 'title': str(info.get('title') or video_id)[:256],
            'artist': str(info.get('artist') or info.get('uploader') or '')[:256],
            'duration_ms': round(duration * 1000)}


class DownloadFailure(Exception):
    """Only fixed error codes are exposed; upstream bodies may contain secrets."""


class LavalinkClient:
    """Read bounded chunks to EOF; Lavalink sends a sentinel Content-Length."""
    def __init__(self, base, password):
        import urllib.request
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.base, self.password = base, password

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def stream(self, method, path, params):
        import urllib.request
        import urllib.error
        from urllib.parse import urlencode
        request = urllib.request.Request(self.base + path + '?' + urlencode(params),
            headers={'Authorization': self.password}, method=method)
        try:
            response = self.opener.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        class Response:
            status_code = response.code
            def __enter__(self):
                return self
            def __exit__(self, *args):
                response.close()
            def iter_bytes(self, size):
                while chunk := response.read(size):
                    yield chunk
            def json(self):
                return json.loads(response.read(1024 * 1024))
        return Response()

    def get(self, path, params):
        with self.stream('GET', path, params) as response:
            # Metadata is small; retain only the parsed response.
            data = response.json() if response.status_code == 200 else None
            from types import SimpleNamespace
            return SimpleNamespace(status_code=response.status_code, json=lambda: data)


def download(video_id, directory, max_seconds, *, client=None):
    import os
    import time
    import subprocess
    from contextlib import nullcontext

    base = os.environ.get('ONAIR_LAVALINK_URL', 'http://127.0.0.1:2333').rstrip('/')
    password = os.environ.get('ONAIR_LAVALINK_PASSWORD')
    if not password:
        raise DownloadFailure('service_auth')
    root = Path(directory)
    # Never follow redirects with the internal Lavalink password.
    context = nullcontext(client) if client else LavalinkClient(base, password)
    with context as connection:
        response = connection.get('/v4/loadtracks', params={
            'identifier': 'https://www.youtube.com/watch?v=' + video_id})
        if response.status_code in (401, 403):
            raise DownloadFailure('service_auth')
        if response.status_code != 200:
            raise DownloadFailure('service_unavailable')
        payload = response.json()
        metadata = None
        if payload.get('loadType') == 'track':
            info = payload['data']['info']
            metadata = validate_metadata(dict(id=info['identifier'], title=info.get('title'),
                uploader=info.get('author'), duration=info['length'] / 1000,
                is_live=info.get('isStream', False)), video_id, max_seconds)
        # WEB metadata can be blocked while OAuth TV playback works. Use the
        # requested ID as display title and validate actual file duration below.
        source = root / 'source.media'
        size = 0
        start = time.monotonic()
        with connection.stream('GET', '/youtube/stream/' + video_id,
                               params={'withClient': 'TVHTML5'}) as response:
            if response.status_code in (401, 403):
                raise DownloadFailure('service_auth')
            if response.status_code != 200:
                # A 500 here can mean cipher, OAuth, IP or video failure: do not
                # claim it proves the Google token expired.
                raise DownloadFailure('stream_failed_check_lavalink')
            with source.open('wb') as output:
                for chunk in response.iter_bytes(65536):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise DownloadFailure('size_limit')
                    if time.monotonic() - start > 90:
                        raise DownloadFailure('download_timeout')
                    output.write(chunk)
        # Lavalink uses an unbounded Content-Length sentinel. Read to EOF and
        # enforce the real byte count instead of trusting that header.
        if not size:
            raise DownloadFailure('empty_stream')
        probe = subprocess.run([os.environ.get('ONAIR_FFPROBE', 'ffprobe'), '-v', 'error',
            '-show_entries', 'format=duration:stream=codec_type', '-of', 'json', str(source)],
            capture_output=True, text=True, timeout=15, check=True)
        measured = json.loads(probe.stdout)
        if not any(stream.get('codec_type') == 'audio' for stream in measured.get('streams', [])):
            raise DownloadFailure('no_audio')
        actual = validate_metadata(dict(id=video_id, duration=float(measured['format']['duration'])),
                                   video_id, max_seconds)
        if metadata is None:
            metadata = actual
        else:
            metadata['duration_ms'] = actual['duration_ms']
        metadata['source'] = source.name
        (root / 'result.json').write_text(json.dumps(metadata), encoding='utf-8')


if __name__ == '__main__':
    try:
        download(sys.argv[1], sys.argv[2], int(sys.argv[3]))
    except Exception as exc:
        import httpx
        import urllib.error
        code = str(exc) if isinstance(exc, DownloadFailure) else (
            'media_tools_unavailable' if isinstance(exc, FileNotFoundError) else
            'service_unavailable' if isinstance(exc, (httpx.RequestError, urllib.error.URLError,
                                                     TimeoutError, ConnectionError)) else 'invalid_media')
        (Path(sys.argv[2]) / 'error.json').write_text(json.dumps({'code': code}), encoding='utf-8')
        sys.exit(1)
