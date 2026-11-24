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

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from redis.exceptions import RedisError

from .auth import get_broadcast_host
from .messaging.contracts import Segment, validate_audio
from .song_fit import song_fits
from .station_runtime import station_runtime
from .stations import User
from .streaming.tracks import MUSIC_SEGMENT_PREFIX, REGISTER, now_playing
from .track_names import clean_names

router = APIRouter(prefix='/api/stations')
search_router = APIRouter(prefix='/api/tracks')
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


def search_candidates(payload, max_seconds, limit=3):
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
        if len(result) == limit:
            break
    return result


async def search_youtube(settings, query, limit=3):
    """YouTube 검색 — 신청곡 검색과 DJ 선곡(dj_picks.py)이 함께 쓴다. 실패하면 HTTPException(502·503·504)."""
    if not settings.lavalink_password:
        raise HTTPException(503, 'Search service unavailable')
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = await client.get(settings.lavalink_url + '/v4/loadtracks',
                headers={'Authorization': settings.lavalink_password},
                params={'identifier': 'ytsearch:' + query})
        if response.status_code != 200:
            raise HTTPException(503, 'Search service unavailable')
        return search_candidates(response.json(), settings.youtube_max_seconds, limit)
    except httpx.TimeoutException:
        raise HTTPException(504, 'Search timed out') from None
    except httpx.RequestError:
        raise HTTPException(503, 'Search service unavailable') from None
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(502, 'Invalid search service response') from None


@router.get('/{station_id}/tracks/queue')
async def track_queue(station_id: str, request: Request):
    """Public: the actual upcoming music queue, in playback order.

    Reads the shared playback queue (speech + music segment ids) and keeps only the
    ids that are music submissions, preserving order. Already-played entries are gone
    from the shared queue by the time the HLS publisher consumes them, so this is
    naturally "what's left to play," not full history. The song listeners hear right now has
    already left that queue, so it comes separately as nowPlaying (None once it ends).
    """
    station_runtime(request, station_id)
    prefix = f'backend:{station_id}'
    redis = request.app.state.redis
    try:
        async with asyncio.timeout(3):
            ids = await redis.lrange(prefix + ':queue', 0, 199)
            submissions = await redis.hmget(prefix + ':music-submissions', ids) if ids else []
            playing = await redis.hget(prefix + ':track-plan', 'playing')
            current = await now_playing(redis, prefix, time.time())
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    queue = [json.loads(raw) for raw in submissions if raw]
    return {'queue': queue, 'playingSegmentId': playing, 'nowPlaying': current}


@router.get('/{station_id}/tracks/access')
async def track_access(station_id: str, request: Request, user: User):
    """Expose request readiness without exposing the host's private station record."""
    station_runtime(request, station_id)
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
    station_runtime(request, station_id)
    try:
        async with asyncio.timeout(3):
            raw = await request.app.state.redis.get(f'backend:{station_id}:station')
        if not raw:
            raise HTTPException(404, 'Station not found')
        station = json.loads(raw)
        if station['status'] != 'started':
            raise HTTPException(409, 'Station must be started')
        return TrackSearchResult(candidates=await search_youtube(settings, body.query))
    except (httpx.TimeoutException, TimeoutError):
        raise HTTPException(504, 'Search timed out') from None
    except (httpx.RequestError, RedisError):
        raise HTTPException(503, 'Search service unavailable') from None
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(502, 'Invalid search service response') from None


@search_router.post('/search', response_model=TrackSearchResult,
    responses={401: {'description': 'Login required'}, 403: {'description': 'Guests cannot create broadcasts'},
               503: {'description': 'Search service unavailable'}, 504: {'description': 'Search timed out'}})
async def search_before_broadcast(body: TrackSearchInput, request: Request,
                                  host: Annotated[dict, Depends(get_broadcast_host)]):
    """방송 만들기 화면에서 첫 노래를 검색으로 고른다 (#346). 방송이 아직 없어 방송별 검색을 쓸 수 없다.

    방송을 만들 수 있는 계정만 쓴다(게스트 403). 응답은 방송별 검색과 같고, 고른 후보의 url을 first_song_url로 보낸다.
    """
    return TrackSearchResult(candidates=await search_youtube(request.app.state.settings, body.query))


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
    comment: str | None = Field(default=None, max_length=60)
    anonymous: bool = False

    @field_validator('url')
    @classmethod
    def validate_url(cls, value):
        youtube_id(value)
        return value

    @field_validator('comment')
    @classmethod
    def clean_comment(cls, value):
        return (value or '').strip() or None

    @model_validator(mode='after')
    def comment_needs_song_request(self):
        if (self.comment or self.anonymous) and (self.source != 'request' or self.request_id is not None):
            raise ValueError('comment and anonymous are only valid for a song request')
        return self


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
-- The engine attaches the comment when track.queued arrives, so it must be sent first.
if ARGV[11] and ARGV[11] ~= '' then
 redis.call('XADD', KEYS[7], '*', 'type', 'request.arrived', 'contract_version', '2',
  'event_id', 'evt_comment_' .. ARGV[1], 'station_id', ARGV[6], 'at', ARGV[7], 'payload', ARGV[11])
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


# 뒤가 잘린 음원 (#339): Lavalink youtube 플러그인의 /youtube/stream은 길이가 적히지 않은 형식(itag 18, 지금 받는
# 유일한 형식)을 첫 구간 약 11.86MB까지만 준다. 영상 비트레이트가 높은 곡은 2~5분에서 끊긴다. 같은 영상은 다시 받아도
# 같은 자리에서 끊기므로 다시 받지 않는다. 받은 파일의 머리에는 원래 길이가 적혀 있어서, 변환한 MP3를 실제로 재야 안다.
TRUNCATED_TOLERANCE_SEC = 5.0

# 잘린 영상 대신 받을 영상 (#339): 같은 곡의 음원·가사 영상은 화면이 거의 멈춰 있어 용량이 작고 끝까지 받아진다.
# 제목으로 다시 찾아, 길이가 비슷하고 제목이 겹치고 판(라이브·리믹스·커버 등)이 같은 것만 쓴다. 맞는 것이 없으면
# 엉뚱한 곡을 트느니 거절한다. 운영 서버에서 3~7분 영상 30편 중 8편이 잘렸다 (2026-11-22, #339 댓글).
SUBSTITUTE_SEARCH = 10           # 검색 결과에서 살펴볼 후보 수
SUBSTITUTE_TRIES = 2             # 실제로 받아 볼 후보 수 — 한 번에 수 초가 걸리고 그동안 다른 다운로드가 막힌다
SUBSTITUTE_LENGTH_TOLERANCE_SEC = 15.0
SUBSTITUTE_TITLE_OVERLAP = 0.5   # 원래 제목의 낱말 중 후보의 제목·채널에 있어야 하는 비율
_BRACKETS = re.compile(r'[()\[\]{}【】「」『』<>《》〈〉]')
_WORD = re.compile(r'[0-9a-z]+|[가-힣]+|[぀-ヿ一-鿿]+')
# 곡이 무엇인지와 상관없이 영상 제목에 붙는 말 — 찾을 때와 견줄 때 뺀다
_TITLE_NOISE = frozenset((
    'official', 'music', 'video', 'audio', 'mv', 'm', 'v', 'lyric', 'lyrics', 'visualizer', 'hd', 'hq', '4k',
    'remaster', 'remastered', 'full', 'ver', 'version', 'feat', 'ft', 'prod',
    '뮤직비디오', '뮤비', '공식', '가사', '음원', '고음질'))
# 같은 곡이라도 다른 녹음임을 알리는 말 — 원래 영상과 후보에서 같아야 한다
_VERSION_WORDS = frozenset((
    'live', 'remix', 'cover', 'karaoke', 'instrumental', 'inst', 'mr', 'acoustic', 'nightcore', 'slowed', 'sped',
    'reverb', 'reaction', 'concert', 'session', 'demo', 'mashup', 'medley', 'mix', 'edit', 'clean', 'bass', 'boosted',
    '라이브', '리믹스', '커버', '노래방', '반주', '직캠', '무대', '콘서트', '어쿠스틱', '메들리', '모음'))
_ALTERED = re.compile(r'[0-9]+(d|hz|k?bps)')
# 화면이 멈춰 있는 영상임을 알리는 말 — 이런 후보를 먼저 받아 본다
_STILL_WORDS = frozenset(('audio', 'lyric', 'lyrics', 'topic', '가사', '음원'))


def title_words(text):
    return _WORD.findall(_BRACKETS.sub(' ', text.lower()))


def substitute_query(title):
    """원래 영상의 제목에서 영상 종류를 알리는 말을 빼고 음원을 찾는 검색어를 만든다. 판을 알리는 말은 남긴다."""
    kept = [word for word in title_words(title) if word not in _TITLE_NOISE]
    return ' '.join(kept + ['audio'])[:120]


def version_marks(words):
    """제목 낱말 가운데 어떤 녹음인지 알리는 것. "8d"·"12d"·"432hz"처럼 소리를 손본 표시도 여기에 든다."""
    return {word for word in words if word in _VERSION_WORDS or _ALTERED.fullmatch(word)}


def substitute_order(candidates, video_id, title, expected_ms, channel=''):
    """검색 결과에서 원래 영상 대신 받아 볼 후보를 고르고 차례를 정한다.

    차례: 원래 영상과 같은 채널이 올린 것(가수 채널의 음원·가사 영상), 음원·가사 영상, 길이가 가까운 것.
    """
    wanted = [word for word in title_words(title) if word not in _TITLE_NOISE]
    if not wanted:
        return []
    version = version_marks(wanted)
    kept = []
    for candidate in candidates:
        words = set(title_words(candidate.title))
        gap = abs(candidate.duration_ms - expected_ms)
        if (candidate.video_id == video_id or gap > SUBSTITUTE_LENGTH_TOLERANCE_SEC * 1000
                or version_marks(words) != version):
            continue
        known = words.union(title_words(candidate.channel))
        if sum(word in known for word in wanted) < SUBSTITUTE_TITLE_OVERLAP * len(wanted):
            continue
        other_channel = not channel or candidate.channel.strip().lower() != channel.strip().lower()
        kept.append((other_channel, not _STILL_WORDS.intersection(known), gap, len(kept), candidate))
    return [row[-1] for row in sorted(kept, key=lambda row: row[:4])]


def ffprobe_path(settings):
    configured = os.environ.get('ONAIR_FFPROBE')
    if configured:
        return configured
    if Path(settings.ffmpeg).is_absolute():
        return str(Path(settings.ffmpeg).with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe'))
    return 'ffprobe'


async def audio_seconds(ffprobe, path):
    """파일에 실제로 들어 있는 오디오의 길이."""
    try:
        process = await asyncio.create_subprocess_exec(
            ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except FileNotFoundError:
        raise HTTPException(503, 'Media tools unavailable; contact the administrator') from None
    try:
        async with asyncio.timeout(15):
            output, _ = await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    try:
        seconds = float(output.decode().strip())
    except ValueError:
        seconds = 0.0
    if process.returncode or seconds <= 0:
        raise HTTPException(502, 'Audio preparation failed; the video may be unavailable or unsupported')
    return seconds


def music_segment_id(generation, owner, retry_key):
    # A reused client key in a new broadcast must not inherit an old PLAYED state.
    scope = f'{generation}:' if generation else ''
    return MUSIC_SEGMENT_PREFIX + hashlib.sha256(f'{scope}{owner}:{retry_key}'.encode()).hexdigest()


class MusicService:
    def __init__(self, redis, settings, lock=None):
        self.redis, self.settings = redis, settings
        self.prefix = f'backend:{settings.station_id}'
        # 채널끼리 같이 쓴다 — 다운로드·변환은 서버 전체에서 한 번에 하나
        self.lock = lock or asyncio.Lock()

    def is_cached(self, segment_id):
        directory = self.settings.audio_dir / self.settings.station_id / 'youtube'
        return (directory / (segment_id + '.mp3')).is_file() and (directory / (segment_id + '.json')).is_file()

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
                        metadata['title'], metadata['artist'] = clean_names(*enriched)
                        manifest.write_text(json.dumps(metadata), encoding='utf-8')
                return audio, metadata
            raise HTTPException(409, 'Idempotency-Key already used for another video')
        if not self.settings.lavalink_password:
            raise HTTPException(503, 'Lavalink is not configured')
        with tempfile.TemporaryDirectory(prefix='onair-youtube-') as temp:
            converted, metadata, expected_ms = await self.download(video_id, Path(temp))
            if metadata['duration_ms'] < expected_ms - TRUNCATED_TOLERANCE_SEC * 1000:
                # 그대로 내보내면 곡이 도중에 끊기고 다음 멘트가 나온다. 이 영상은 쓰지 않는다
                logger.warning('YouTube audio for %s is cut short: got %.1fs of %.1fs', video_id,
                               metadata['duration_ms'] / 1000, expected_ms / 1000)
                replacement = await self.substitute(video_id, metadata, expected_ms, Path(temp))
                if replacement is None:
                    raise HTTPException(502, {'code': 'audio_truncated', 'message':
                                              'Only part of this video could be downloaded; try another upload'})
                # 곡 정보(track_ref·제목·가수)는 신청한 영상 그대로 둔다 — 같은 곡이고, 재시도·캐시·신청 기록이
                # 신청한 영상으로 묶여 있다. 길이만 실제로 나갈 소리의 것으로 바꾼다
                converted, metadata['duration_ms'] = replacement
            # Stage beside the destination so publication uses a same-volume rename on Windows.
            staged = audio.with_suffix('.tmp')
            await asyncio.to_thread(shutil.copyfile, converted, staged)
            staged.replace(audio)
            if metadata['title'] != video_id:
                # 영상 제목과 채널 이름 그대로면 화면에 그대로 보이고 DJ도 그대로 읽는다
                metadata['title'], metadata['artist'] = clean_names(metadata['title'], metadata['artist'])
            manifest.write_text(json.dumps(metadata), encoding='utf-8')
        return audio, metadata

    async def substitute(self, video_id, metadata, expected_ms, root):
        """잘린 영상 대신 같은 곡의 다른 영상을 받는다 → (MP3 경로, 실제 길이 ms). 쓸 만한 것이 없으면 None."""
        if metadata['title'] == video_id:
            return None  # 제목을 알아내지 못한 영상은 무엇을 찾을지 모른다
        try:
            found = await search_youtube(self.settings, substitute_query(metadata['title']), SUBSTITUTE_SEARCH)
        except HTTPException:
            return None
        chosen = substitute_order(found, video_id, metadata['title'], expected_ms,
                                  metadata.get('artist', ''))[:SUBSTITUTE_TRIES]
        for index, candidate in enumerate(chosen):
            folder = root / f'substitute-{index}'
            folder.mkdir()
            try:
                converted, other, declared_ms = await self.download(candidate.video_id, folder)
            except HTTPException:
                continue
            if other['duration_ms'] < declared_ms - TRUNCATED_TOLERANCE_SEC * 1000:
                logger.warning('Substitute %s for %s is cut short too', candidate.video_id, video_id)
                continue
            logger.warning('Playing %s (%s) in place of cut-short %s', candidate.video_id, candidate.title, video_id)
            return converted, other['duration_ms']
        return None

    async def download(self, video_id, root):
        """한 번 받아 MP3로 바꾼다 → (MP3 경로, 곡 정보, 원래 길이 ms). 곡 정보의 duration_ms는 MP3를 실제로 잰 길이다.

        엔진은 track.started + duration_ms를 곡이 끝나는 시각으로 쓰므로(계약 3.4절) 실제로 나갈 길이를 알린다.
        """
        env = os.environ | {'ONAIR_LAVALINK_URL': self.settings.lavalink_url,
                            'ONAIR_LAVALINK_PASSWORD': self.settings.lavalink_password,
                            'ONAIR_FFPROBE': ffprobe_path(self.settings)}
        try:
            await run_process([sys.executable, '-m', 'app.youtube_worker', video_id, str(root),
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
        expected_ms = metadata['duration_ms']
        metadata['duration_ms'] = round(await audio_seconds(ffprobe_path(self.settings), converted) * 1000)
        return converted, metadata, expected_ms

    async def enqueue(self, video_id, owner, retry_key, source='playlist', request_id=None, nickname=None, comment=None,
                      extra=None):
        """extra는 track.queued에 더 실을 값이다 — DJ 선곡의 suggest_id·candidate (3.4절, #295)."""
        station_raw = await self.redis.get(self.prefix + ':station')
        station = json.loads(station_raw) if station_raw else {}
        generation = station.get('generation', '')
        created_at = station.get('created_at')
        segment_id = music_segment_id(generation, owner, retry_key)
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
            event = {'track': event_track, 'source': source, **(extra or {})}
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
            arrived = None
            if song_request_id and comment:
                # Not stored on the public request entry: the engine has not screened it yet.
                arrived = dict(request_id=song_request_id, kind='song_request', body=comment,
                               requester_ref=owner, track=event_track)
                if nickname:
                    arrived['nickname'] = nickname
            async with asyncio.timeout(3):
                raw = await self.redis.eval(ENQUEUE, 11, self.prefix + ':music-submissions',
                    self.prefix + ':segments', self.prefix + ':queue', self.prefix + ':station',
                    self.prefix + ':tracks', self.prefix + ':track-plan', f'engine:{self.settings.station_id}:in',
                    self.prefix + ':playlist', self.prefix + ':playlist-order',
                    self.prefix + ':requests', self.prefix + ':request-history:' + segment_id,
                    segment_id, json.dumps(segment.model_dump(), sort_keys=True, ensure_ascii=False),
                    json.dumps(result), owner, json.dumps(event), self.settings.station_id, str(time.time()),
                    generation, str(created_at) if created_at is not None else '',
                    json.dumps(entry, ensure_ascii=False) if entry else '',
                    json.dumps(arrived, ensure_ascii=False) if arrived else '')
            if raw == 'unavailable':
                raise HTTPException(409, 'Station is no longer started')
            return json.loads(raw)


@router.post('/{station_id}/tracks', status_code=202, responses={
    401: {'description': 'Login required'}, 409: {'description': 'Station or retry-key conflict'},
    422: {'description': 'Invalid link, or detail.code "song_not_fit": the song does not fit the station'},
    429: {'description': 'Download busy'}, 502: {'description': 'Audio preparation failed'},
    503: {'description': 'HLS, Redis or media tools unavailable'}, 504: {'description': 'Preparation timed out'}})
async def enqueue_track(station_id: str, body: TrackInput, request: Request, user: User,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9._:-]+$')]):
    """Prepare a single YouTube video and append it to the live queue; 202 is not playback confirmation."""
    settings = request.app.state.settings
    runtime = station_runtime(request, station_id)
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
            return await runtime.music.enqueue(youtube_id(body.url), user['id'], idempotency_key)
        # 청취자 신청곡은 음원을 받기 전에 방의 장르·분위기에 어울리는지 엔진에 묻는다 (계약 6.4절, #301).
        # 호스트의 플레이리스트와 투표 결과 곡은 묻지 않는다. 프론트는 상태 코드가 아니라 code로 가린다
        if body.source == 'request' and not await song_fits(
                getattr(request.app.state, 'engine_http', None), station_id, youtube_id(body.url),
                fetch_oembed_title):
            raise HTTPException(422, {'code': 'song_not_fit',
                                      'message': 'This song does not fit the station'})
        nickname = None if body.anonymous else user.get('name')
        return await runtime.music.enqueue(youtube_id(body.url), user['id'], idempotency_key,
                                          body.source, body.request_id, nickname, body.comment)
    except RedisError:
        raise HTTPException(503, 'Redis unavailable; retry with the same Idempotency-Key') from None
    except TimeoutError:
        raise HTTPException(504, 'Preparation timed out; retry with the same Idempotency-Key') from None
    except OSError:
        raise HTTPException(503, 'Media tools or audio storage unavailable') from None
