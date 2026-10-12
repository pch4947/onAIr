"""Isolated, bounded YouTube download; invoked by the backend, never with shell=True."""
import json
import math
from pathlib import Path
import sys


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


def download(video_id, directory, max_seconds):
    from yt_dlp import YoutubeDL

    root = Path(directory)
    def match(info, *, incomplete=False):
        if not incomplete:
            validate_metadata(info, video_id, max_seconds)
        return None

    def limit_bytes(progress):
        if progress.get('downloaded_bytes', 0) > 100 * 1024 * 1024:
            raise ValueError('Audio download exceeds size limit')

    options = dict(quiet=True, no_warnings=True, noplaylist=True, cachedir=False,
                   js_runtimes={'node': {}},
                   # Native HTTPS download only: no external downloader or child process.
                   format='bestaudio[protocol=https]/best[protocol=https]',
                   outtmpl=str(root / 'source.%(ext)s'), socket_timeout=15,
                   retries=1, fragment_retries=1, max_filesize=100 * 1024 * 1024,
                   match_filter=match, progress_hooks=[limit_bytes])
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info('https://www.youtube.com/watch?v=' + video_id, download=True)
        metadata = validate_metadata(info, video_id, max_seconds)
        source = Path(ydl.prepare_filename(info)).resolve()
        if source.parent != root.resolve() or not source.is_file() or source.stat().st_size == 0:
            raise ValueError('Audio download incomplete')
        metadata['source'] = source.name
        (root / 'result.json').write_text(json.dumps(metadata), encoding='utf-8')


if __name__ == '__main__':
    download(sys.argv[1], sys.argv[2], int(sys.argv[3]))
