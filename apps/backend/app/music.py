"""Owner-only direct YouTube playout MVP, using the existing durable HLS queue."""
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from redis.exceptions import RedisError

from .messaging.contracts import Segment, validate_audio
from .stations import User
from .streaming.tracks import REGISTER

router = APIRouter(prefix='/api/stations')
logger = logging.getLogger(__name__)


def youtube_id(value):
    try:
        url = urlsplit(value)
        if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443):
            raise ValueError('Invalid YouTube URL')
        if url.hostname == 'youtu.be':
            video = url.path.removeprefix('/')
        elif url.hostname in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
            if url.path == '/watch':
                values = parse_qs(url.query).get('v', [])
                video = values[0] if len(values) == 1 else ''
            elif url.path.startswith(('/shorts/', '/embed/')):
                video = url.path.split('/')[-1]
            else:
                video = ''
        else:
            video = ''
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video):
            raise ValueError('Invalid YouTube video ID')
        return video
    except ValueError:
        raise ValueError('Use an HTTPS YouTube video link (watch, youtu.be, shorts or embed)') from None


class TrackInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(max_length=2048)
    source: Literal['playlist', 'request', 'vote'] = 'playlist'
    request_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator('url')
    @classmethod
    def validate_url(cls, value):
        youtube_id(value)
        return value


# Validate key types before any write: Lua errors do not roll back earlier writes.
ENQUEUE = """
local kinds = {'hash', 'hash', 'list', 'string', 'hash', 'hash', 'stream', 'hash', 'list'}
for i=1,9 do
 local t = redis.call('TYPE', KEYS[i]).ok
 if t ~= 'none' and t ~= kinds[i] then return redis.error_reply('Unexpected music key type') end
end
local station = redis.call('GET', KEYS[4])
if not station then return 'unavailable' end
local s = cjson.decode(station)
if s.owner_id ~= ARGV[4] or s.status ~= 'started' then return 'unavailable' end
local old = redis.call('HGET', KEYS[1], ARGV[1])
if old then return old end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
redis.call('RPUSH', KEYS[3], ARGV[1])
redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
""" + REGISTER + "return ARGV[3]"


async def run_process(args, timeout, *, env=None):
    process = await asyncio.create_subprocess_exec(
        *args, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(timeout):
            await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        raise HTTPException(502, 'Audio preparation failed; the video may be unavailable or unsupported')


class MusicService:
    def __init__(self, redis, settings):
        self.redis, self.settings = redis, settings
        self.prefix = f'backend:{settings.station_id}'
        self.lock = asyncio.Lock()

    async def prepare(self, video_id, segment_id):
        directory = self.settings.audio_dir / self.settings.station_id / 'youtube'
        directory.mkdir(parents=True, exist_ok=True)
        audio = directory / (segment_id + '.mp3')
        manifest = directory / (segment_id + '.json')
        if audio.is_file() and manifest.is_file():
            metadata = json.loads(manifest.read_text(encoding='utf-8'))
            if metadata['track_ref'] == 'yt:' + video_id:
                return audio, metadata
            raise HTTPException(409, 'Idempotency-Key already used for another video')
        with tempfile.TemporaryDirectory(prefix='onair-youtube-') as temp:
            root = Path(temp)
            if not self.settings.lavalink_password:
                raise HTTPException(503, 'Lavalink is not configured')
            env = os.environ | {'ONAIR_LAVALINK_URL': self.settings.lavalink_url,
                                'ONAIR_LAVALINK_PASSWORD': self.settings.lavalink_password}
            if Path(self.settings.ffmpeg).is_absolute():
                env.setdefault('ONAIR_FFPROBE', str(Path(self.settings.ffmpeg).with_name(
                    'ffprobe.exe' if os.name == 'nt' else 'ffprobe')))
            try:
                await run_process([sys.executable, '-m', 'app.youtube_worker', video_id, temp,
                                   str(self.settings.youtube_max_seconds)], 120, env=env)
            except HTTPException:
                failure = root / 'error.json'
                code = json.loads(failure.read_text())['code'] if failure.exists() else 'worker_failed'
                logger.warning('YouTube preparation failed: %s', code)
                if code in ('service_unavailable', 'service_auth', 'media_tools_unavailable'):
                    raise HTTPException(503, 'Lavalink unavailable; contact the administrator') from None
                raise
            metadata = json.loads((root / 'result.json').read_text(encoding='utf-8'))
            source = (root / metadata.pop('source')).resolve()
            if source.parent != root.resolve() or not source.is_file():
                raise HTTPException(502, 'Invalid downloaded audio')
            converted = root / 'audio.mp3'
            await run_process([self.settings.ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error',
                               '-y', '-i', str(source), '-vn', '-t', str(self.settings.youtube_max_seconds),
                               '-ar', '48000', '-ac', '2', '-c:a', 'libmp3lame', '-b:a', '192k',
                               str(converted)], 120)
            # Stage beside the destination so publication uses a same-volume rename on Windows.
            staged = audio.with_suffix('.tmp')
            await asyncio.to_thread(shutil.copyfile, converted, staged)
            staged.replace(audio)
            manifest.write_text(json.dumps(metadata), encoding='utf-8')
        return audio, metadata

    async def enqueue(self, video_id, owner, retry_key, source='playlist', request_id=None):
        segment_id = 'music_' + hashlib.sha256(f'{owner}:{retry_key}'.encode()).hexdigest()
        async def existing():
            raw = await self.redis.hget(self.prefix + ':music-submissions', segment_id)
            if raw:
                result = json.loads(raw)
                if (result['track']['track_ref'] != 'yt:' + video_id
                        or result.get('source', 'playlist') != source or result.get('request_id') != request_id):
                    raise HTTPException(409, 'Idempotency-Key already used for another video')
                return result
        async with asyncio.timeout(3):
            old = await existing()
        if old:
            return old
        if self.lock.locked():
            raise HTTPException(429, 'Another audio download is in progress; retry later', headers={'Retry-After': '5'})
        async with self.lock:
            audio, track = await self.prepare(video_id, segment_id)
            segment = Segment(id=segment_id, station_id=self.settings.station_id,
                audio_ref=audio.relative_to(self.settings.audio_dir).as_posix(), duration_ms=track['duration_ms'],
                corner_type='music', kind='music', priority=50, reorderable=False,
                request_ref=None, music_ref=track['track_ref'], created_at=time.time())
            validate_audio(self.settings.audio_dir, segment)
            result = dict(segment_id=segment_id, track=track, status='queued',
                          hls_url=f'/hls/{self.settings.station_id}/index.m3u8', source=source, request_id=request_id)
            event_track = {**track, 'title': track['title'][:200], 'artist': track['artist'][:200]}
            event = {'track': event_track, 'source': source}
            if request_id:
                event['request_id'] = request_id
            async with asyncio.timeout(3):
                raw = await self.redis.eval(ENQUEUE, 9, self.prefix + ':music-submissions',
                    self.prefix + ':segments', self.prefix + ':queue', self.prefix + ':station',
                    self.prefix + ':tracks', self.prefix + ':track-plan', f'engine:{self.settings.station_id}:in',
                    self.prefix + ':playlist', self.prefix + ':playlist-order',
                    segment_id, json.dumps(segment.model_dump(), sort_keys=True, ensure_ascii=False),
                    json.dumps(result), owner, json.dumps(event), self.settings.station_id, str(time.time()))
            if raw == 'unavailable':
                raise HTTPException(409, 'Station is no longer started')
            return json.loads(raw)


@router.post('/{station_id}/tracks', status_code=202, responses={
    403: {'description': 'Station owner required'}, 409: {'description': 'Station or retry-key conflict'},
    429: {'description': 'Download busy'}, 502: {'description': 'Audio preparation failed'},
    503: {'description': 'HLS, Redis or media tools unavailable'}, 504: {'description': 'Preparation timed out'}})
async def enqueue_track(station_id: str, body: TrackInput, request: Request, user: User,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9._:-]+$')]):
    """Prepare a single YouTube video and append it to the live queue; 202 is not playback confirmation."""
    settings = request.app.state.settings
    if station_id != settings.station_id:
        raise HTTPException(404, 'Station not found')
    if not settings.hls_enabled:
        raise HTTPException(503, 'HLS is disabled')
    try:
        async with asyncio.timeout(3):
            raw = await request.app.state.redis.get(f'backend:{station_id}:station')
        if not raw:
            raise HTTPException(404, 'Station not found')
        station = json.loads(raw)
        if station['owner_id'] != user['id']:
            raise HTTPException(403, 'Station owner required')
        if station['status'] != 'started':
            raise HTTPException(409, 'Station must be started')
        if body.source == 'playlist' and body.request_id is None:
            return await request.app.state.music.enqueue(youtube_id(body.url), user['id'], idempotency_key)
        return await request.app.state.music.enqueue(youtube_id(body.url), user['id'], idempotency_key,
                                                     body.source, body.request_id)
    except RedisError:
        raise HTTPException(503, 'Redis unavailable; retry with the same Idempotency-Key') from None
    except TimeoutError:
        raise HTTPException(504, 'Preparation timed out; retry with the same Idempotency-Key') from None
    except OSError:
        raise HTTPException(503, 'Media tools or audio storage unavailable') from None
