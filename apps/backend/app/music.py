"""Authenticated YouTube playout MVP, using the existing durable HLS queue."""
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
import httpx
import time
from datetime import datetime, timezone
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


class TrackSearchInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=120)

    @field_validator('query')
    @classmethod
    def clean_query(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('Enter a search term')
        return value


class TrackCandidate(BaseModel):
    video_id: str
    url: str
    title: str
    channel: str
    duration_ms: int
    thumbnail_url: str


class TrackSearchResult(BaseModel):
    candidates: list[TrackCandidate]


def search_candidates(payload, max_seconds):
    if payload.get('loadType') == 'empty':
        return []
    if payload.get('loadType') != 'search' or not isinstance(payload.get('data'), list):
        raise ValueError('Invalid search response')
    result, seen = [], set()
    for row in payload['data']:
        if not isinstance(row, dict) or not isinstance(row.get('info'), dict):
            continue
        info = row['info']
        video = info.get('identifier', '')
        duration = info.get('length')
        if (not isinstance(video, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video)
                or video in seen or info.get('isStream') is not False
                or info.get('sourceName') != 'youtube'
                or type(duration) is not int or not 0 < duration <= max_seconds * 1000):
            continue
        seen.add(video)
        result.append(TrackCandidate(video_id=video, url='https://www.youtube.com/watch?v=' + video,
            title=str(info.get('title') or video)[:200], channel=str(info.get('author') or '')[:200],
            duration_ms=duration, thumbnail_url=f'https://i.ytimg.com/vi/{video}/mqdefault.jpg'))
        if len(result) == 3:
            break
    return result


@router.get('/{station_id}/tracks/queue')
async def track_queue(station_id: str, request: Request):
    """Public: the actual upcoming music queue, in playback order.

    Reads the shared playback queue (speech + music segment ids) and keeps only the
    ids that are music submissions, preserving order. Already-played entries are gone
    from the shared queue by the time the HLS publisher consumes them, so this is
    naturally "what's left to play," not full history.
    """
    settings = request.app.state.settings
    if station_id != settings.station_id:
        raise HTTPException(404, 'Station not found')
    prefix = f'backend:{station_id}'
    try:
        async with asyncio.timeout(3):
            ids = await request.app.state.redis.lrange(prefix + ':queue', 0, 199)
            submissions = await request.app.state.redis.hmget(prefix + ':music-submissions', ids) if ids else []
            playing = await request.app.state.redis.hget(prefix + ':track-plan', 'playing')
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    queue = [json.loads(raw) for raw in submissions if raw]
    return {'queue': queue, 'playingSegmentId': playing}


@router.get('/{station_id}/tracks/access')
async def track_access(station_id: str, request: Request, user: User):
    """Expose request readiness without exposing the host's private station record."""
    if station_id != request.app.state.settings.station_id:
        raise HTTPException(404, 'Station not found')
    try:
        async with asyncio.timeout(3):
            raw = await request.app.state.redis.get(f'backend:{station_id}:station')
        if not raw:
            raise HTTPException(404, 'Station not found')
        return {'can_request': json.loads(raw)['status'] == 'started'}
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None


@router.post('/{station_id}/tracks/search', response_model=TrackSearchResult,
    responses={401: {'description': 'Login required'},
               503: {'description': 'Search service unavailable'}, 504: {'description': 'Search timed out'}})
async def search_tracks(station_id: str, body: TrackSearchInput, request: Request, user: User):
    """Search YouTube only; return up to three finite videos without downloading or enqueueing."""
    settings = request.app.state.settings
    if station_id != settings.station_id:
        raise HTTPException(404, 'Station not found')
    try:
        async with asyncio.timeout(3):
            raw = await request.app.state.redis.get(f'backend:{station_id}:station')
        if not raw:
            raise HTTPException(404, 'Station not found')
        station = json.loads(raw)
        if station['status'] != 'started':
            raise HTTPException(409, 'Station must be started')
        if not settings.lavalink_password:
            raise HTTPException(503, 'Search service unavailable')
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = await client.get(settings.lavalink_url + '/v4/loadtracks',
                headers={'Authorization': settings.lavalink_password},
                params={'identifier': 'ytsearch:' + body.query})
        if response.status_code != 200:
            raise HTTPException(503, 'Search service unavailable')
        return TrackSearchResult(candidates=search_candidates(response.json(), settings.youtube_max_seconds))
    except (httpx.TimeoutException, TimeoutError):
        raise HTTPException(504, 'Search timed out') from None
    except (httpx.RequestError, RedisError):
        raise HTTPException(503, 'Search service unavailable') from None
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(502, 'Invalid search service response') from None


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
local kinds = {'hash', 'hash', 'list', 'string', 'hash', 'hash', 'stream', 'hash', 'list', 'hash', 'list'}
for i=1,#KEYS do
 local t = redis.call('TYPE', KEYS[i]).ok
 if t ~= 'none' and t ~= kinds[i] then return redis.error_reply('Unexpected music key type') end
end
local station = redis.call('GET', KEYS[4])
if not station then return 'unavailable' end
local s = cjson.decode(station)
if s.status ~= 'started' then return 'unavailable' end
if (s.generation or '') ~= ARGV[8] or (s.created_at and s.created_at ~= tonumber(ARGV[9])) then return 'unavailable' end
local old = redis.call('HGET', KEYS[1], ARGV[1])
if old then return old end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
redis.call('RPUSH', KEYS[3], ARGV[1])
redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
if ARGV[10] and ARGV[10] ~= '' then
 redis.call('HSET', KEYS[10], ARGV[1], ARGV[10])
 redis.call('RPUSH', KEYS[11], cjson.encode({state='requested', at=tonumber(ARGV[7]), source='api', accepted=true}))
end
""" + REGISTER + "return ARGV[3]"


async def fetch_oembed_title(video_id):
    """Best-effort title/author via YouTube's public oEmbed endpoint — no auth, no Lavalink.

    Used only when Lavalink's WEB metadata lookup was blocked and the title fell back to the
    raw video id (docs/BACKEND_LAVALINK.md). Returns None on any failure; callers keep the
    existing fallback.
    """
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get('https://www.youtube.com/oembed',
                params={'url': 'https://www.youtube.com/watch?v=' + video_id, 'format': 'json'})
        if response.status_code != 200:
            return None
        data = response.json()
        title = data.get('title')
        if not title or not isinstance(title, str):
            return None
        author = data.get('author_name')
        return title[:256], (author[:256] if isinstance(author, str) else '')
    except (httpx.HTTPError, ValueError):
        return None


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
                if metadata['title'] == video_id:
                    enriched = await fetch_oembed_title(video_id)
                    if enriched:
                        metadata['title'], metadata['artist'] = enriched
                        manifest.write_text(json.dumps(metadata), encoding='utf-8')
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
            if metadata['title'] == video_id:
                enriched = await fetch_oembed_title(video_id)
                if enriched:
                    metadata['title'], metadata['artist'] = enriched
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

    async def enqueue(self, video_id, owner, retry_key, source='playlist', request_id=None, nickname=None):
        station_raw = await self.redis.get(self.prefix + ':station')
        station = json.loads(station_raw) if station_raw else {}
        generation = station.get('generation', '')
        created_at = station.get('created_at')
        # A reused client key in a new broadcast must not inherit an old PLAYED state.
        scope = f'{generation}:' if generation else ''
        segment_id = 'music_' + hashlib.sha256(f'{scope}{owner}:{retry_key}'.encode()).hexdigest()
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
            # The song and its visible request share an ID and are committed together.
            song_request_id = segment_id if source == 'request' and request_id is None else None
            segment = Segment(id=segment_id, station_id=self.settings.station_id,
                audio_ref=audio.relative_to(self.settings.audio_dir).as_posix(), duration_ms=track['duration_ms'],
                corner_type='music', kind='music', priority=50, reorderable=False,
                request_ref=song_request_id, music_ref=track['track_ref'], created_at=time.time())
            validate_audio(self.settings.audio_dir, segment)
            result = dict(segment_id=segment_id, track=track, status='queued',
                          hls_url=f'/hls/{self.settings.station_id}/index.m3u8', source=source, request_id=request_id)
            event_track = {**track, 'title': track['title'][:200], 'artist': track['artist'][:200]}
            event = {'track': event_track, 'source': source}
            if song_request_id:
                event['nickname'] = nickname or '익명'
                event['request_id'] = song_request_id
            entry = None
            if song_request_id:
                title, artist = track['title'][:200], track['artist'][:200]
                entry = dict(id=song_request_id, prompt=f'{title} - {artist}' if artist else title,
                    listenerId=owner, listenerName=nickname, kind='song_request', status='requested',
                    trackTitle=title, trackArtist=artist or None,
                    trackThumbnailUrl=f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg',
                    createdAt=datetime.now(timezone.utc).isoformat(timespec='milliseconds'))
            if request_id:
                event['request_id'] = request_id
            async with asyncio.timeout(3):
                raw = await self.redis.eval(ENQUEUE, 11, self.prefix + ':music-submissions',
                    self.prefix + ':segments', self.prefix + ':queue', self.prefix + ':station',
                    self.prefix + ':tracks', self.prefix + ':track-plan', f'engine:{self.settings.station_id}:in',
                    self.prefix + ':playlist', self.prefix + ':playlist-order',
                    self.prefix + ':requests', self.prefix + ':request-history:' + segment_id,
                    segment_id, json.dumps(segment.model_dump(), sort_keys=True, ensure_ascii=False),
                    json.dumps(result), owner, json.dumps(event), self.settings.station_id, str(time.time()),
                    generation, str(created_at) if created_at is not None else '',
                    json.dumps(entry, ensure_ascii=False) if entry else '')
            if raw == 'unavailable':
                raise HTTPException(409, 'Station is no longer started')
            return json.loads(raw)


@router.post('/{station_id}/tracks', status_code=202, responses={
    401: {'description': 'Login required'}, 409: {'description': 'Station or retry-key conflict'},
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
        if station['status'] != 'started':
            raise HTTPException(409, 'Station must be started')
        if body.source == 'playlist' and body.request_id is None:
            return await request.app.state.music.enqueue(youtube_id(body.url), user['id'], idempotency_key)
        return await request.app.state.music.enqueue(youtube_id(body.url), user['id'], idempotency_key,
                                                     body.source, body.request_id, user.get('name'))
    except RedisError:
        raise HTTPException(503, 'Redis unavailable; retry with the same Idempotency-Key') from None
    except TimeoutError:
        raise HTTPException(504, 'Preparation timed out; retry with the same Idempotency-Key') from None
    except OSError:
        raise HTTPException(503, 'Media tools or audio storage unavailable') from None
