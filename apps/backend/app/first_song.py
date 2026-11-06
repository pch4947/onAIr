"""방 생성 때 고른 첫 노래 — 음원을 미리 받고 station.created에 곡 정보를 실어 보낸 뒤, 방송 시작과 함께 튼다.

계약(docs/ENGINE_REDIS_CONTRACT.md 3.3·3.4절): 첫 곡은 백엔드가 소개 없이 바로 틀고,
엔진은 그동안 오프닝 멘트를 만들어 "방금 들으신 곡"으로 소개한다.
"""
import asyncio
import logging

from fastapi import HTTPException
from onair_schema import Track
from redis.exceptions import RedisError

from .music import music_segment_id, youtube_id
from .stations import StationStore

log = logging.getLogger(__name__)
PREPARE_TIMEOUT_SEC = 300
FIRST_SONG_SOURCE = 'first'


def first_song_retry_key(generation: str) -> str:
    return f'first-song-{generation}'


async def prepare_first_song(state, record) -> None:
    """음원을 받고 방을 엔진에 넘긴다. 실패하면 첫 노래 없이 넘긴다 — 방송이 멈추면 안 된다."""
    generation, owner = record['generation'], record['owner_id']
    video_id = youtube_id(record['first_song_url'])
    segment_id = music_segment_id(generation, owner, first_song_retry_key(generation))
    track = None
    try:
        async with asyncio.timeout(PREPARE_TIMEOUT_SEC):
            async with state.music.lock:
                _, metadata = await state.music.prepare(video_id, segment_id)
        track = Track.model_validate(metadata).model_dump()
    except Exception:
        # 어떤 오류든 첫 노래 없이 넘긴다. 다시 시도하게 두면 같은 오류로 방송이 끝없이 시작되지 않을 수 있다
        log.warning('First song preparation failed for %s; starting without it', generation, exc_info=True)
    released = await StationStore(state.redis, state.settings.station_id).release(generation, track)
    if released is None:
        log.info('Station %s changed before the first song was ready; not released', generation)


def ensure_first_song_task(state, record) -> None:
    """준비 중인 첫 노래 작업이 없으면 시작한다. 백엔드가 재시작돼도 수명주기 점검이 다시 부른다."""
    if (record.get('status') != 'preparing' or record.get('control_id')
            or record.get('first_song_status') != 'preparing'):
        return
    tasks = getattr(state, 'first_song_tasks', None)
    if tasks is None:
        tasks = state.first_song_tasks = {}
    generation = record['generation']
    if generation in tasks and not tasks[generation].done():
        return
    tasks[generation] = asyncio.create_task(prepare_first_song(state, record), name=f'first-song-{generation}')


async def enqueue_first_song(state, record) -> None:
    """station.started 직후 첫 노래를 대기열에 넣는다. 미리 받아 둔 음원이라 바로 끝난다."""
    song = record.get('config', {}).get('first_song')
    if not song or not getattr(state, 'music', None):
        return
    generation, owner = record['generation'], record['owner_id']
    retry_key = first_song_retry_key(generation)
    # 엔진 이벤트를 처리하는 소비자 안이라 여기서 다시 받으면(몇 분) 방송 전체가 멈춘다
    if not state.music.is_cached(music_segment_id(generation, owner, retry_key)):
        log.warning('First song audio for %s is missing; skipping it', generation)
        return
    try:
        await state.music.enqueue(song['track_ref'].removeprefix('yt:'), owner, retry_key, source=FIRST_SONG_SOURCE)
    except (HTTPException, RedisError, TimeoutError, OSError, ValueError):
        log.warning('Could not queue the first song for %s', generation, exc_info=True)
