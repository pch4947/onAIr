"""DJ 선곡 — 엔진이 낸 곡 후보를 YouTube에서 찾아 대기열에 넣는다 (계약 3.4절 track.suggest, #295).

사람이 플레이리스트를 채우지 않으면 곡이 없어 DJ 멘트만 이어진다. 엔진(DJ)이 방의 장르와 주제에 맞는
곡 후보를 내면, 여기서 신청곡과 같은 검색·받기·대기열 경로로 넣는다.

후보의 제목·아티스트는 LLM이 낸 문자열이라 없는 곡일 수 있다 — 검색어로만 쓴다. 엔진은 여기서 찾은
곡의 정보(track.queued의 track)로 소개한다.
"""
import asyncio
import json
import logging
import time
from uuid import uuid4

import httpx
from fastapi import HTTPException
from redis.exceptions import RedisError

from .messaging.contracts import TrackSuggest
from .music import search_youtube

log = logging.getLogger(__name__)

DJ_SOURCE = 'dj'
DJ_OWNER = 'dj'  # 음원 ID의 소유자 자리 — 사람 계정이 아니다
# 음원 받기는 서버 전체에서 한 번에 하나다(MusicService.lock). 신청곡을 받는 중이면 잠깐 기다린다
LOCK_WAIT_SEC = 60.0
# 후보마다 검색과 받기·변환에 길게는 몇 분이 걸린다. 이 안에 못 넣으면 실패로 답한다 —
# 엔진은 답이 없으면 다시 제안하므로, 엔진의 답 대기(pipeline.song_pick_timeout_sec)보다 짧아야 한다
TOTAL_BUDGET_SEC = 240.0
SEEN_TTL_SEC = 6 * 3600  # 같은 제안의 재전달을 거르는 표시

_tasks: set[asyncio.Task] = set()


def start_pick(state, event: TrackSuggest) -> asyncio.Task:
    """뒤에서 찾아 넣는다 — 엔진 이벤트 소비자 안에서 기다리면 그동안 방송 전체가 멈춘다."""
    task = asyncio.create_task(resolve_pick(state, event))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def resolve_pick(state, event: TrackSuggest) -> str:
    """후보를 차례로 찾아 첫 통과 결과를 대기열에 넣는다. 결과: queued · duplicate · gone · 실패 사유."""
    redis, settings = state.redis, state.settings
    prefix = f'backend:{settings.station_id}'
    try:
        async with asyncio.timeout(3):
            first = await redis.set(f'{prefix}:dj-suggest:{event.suggest_id}', '1', nx=True, ex=SEEN_TTL_SEC)
            if not first:
                return 'duplicate'
            # 틀 곡이 이미 정해져 있다 — 신청곡이나 플레이리스트가 먼저다
            if await redis.hexists(prefix + ':track-plan', 'next'):
                return await _fail(redis, settings.station_id, event.suggest_id, 'not_needed')
    except (RedisError, TimeoutError):
        log.warning('DJ pick %s skipped: Redis unavailable', event.suggest_id, exc_info=True)
        return 'unavailable'
    reason = 'not_found'
    try:
        async with asyncio.timeout(TOTAL_BUDGET_SEC):
            known = await _known_refs(redis, prefix)
            for index, candidate in enumerate(event.candidates):
                query = f'{candidate.artist} {candidate.title}'.strip()[:120]
                try:
                    hits = await search_youtube(settings, query)
                except HTTPException:
                    reason = 'unavailable'
                    continue
                # 검색의 첫 결과만 본다 — 아래로 갈수록 커버·라이브·다른 곡이다. 이미 튼 곡이면 다음 후보로 간다
                hit = next(iter(hits), None)
                if hit is None or 'yt:' + hit.video_id in known:
                    continue
                if not await _wait_unlocked(state.music.lock):
                    reason = 'busy'
                    break
                try:
                    await state.music.enqueue(hit.video_id, DJ_OWNER, f'{event.suggest_id}:{index}', DJ_SOURCE,
                                              extra={'suggest_id': event.suggest_id, 'candidate': index})
                    return 'queued'
                except HTTPException as exc:
                    if exc.status_code == 409:  # 방송이 끝났다 — 알릴 곳이 없다
                        return 'gone'
                    reason = 'busy' if exc.status_code == 429 else 'unavailable'
    except TimeoutError:
        reason = 'unavailable'
    except (RedisError, httpx.HTTPError, OSError, ValueError):
        log.warning('DJ pick %s failed', event.suggest_id, exc_info=True)
        reason = 'unavailable'
    return await _fail(redis, settings.station_id, event.suggest_id, reason)


async def _known_refs(redis, prefix: str) -> set[str]:
    """이번 방송에서 이미 대기열에 넣은 곡(재생했거나 대기 중)의 track_ref."""
    refs = set()
    for raw in (await redis.hvals(prefix + ':tracks')):
        try:
            refs.add(json.loads(raw)['track']['track_ref'])
        except (ValueError, KeyError, TypeError):
            continue
    return refs


async def _wait_unlocked(lock: asyncio.Lock) -> bool:
    deadline = time.monotonic() + LOCK_WAIT_SEC
    while lock.locked():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(1)
    return True


async def _fail(redis, station_id: str, suggest_id: str, reason: str) -> str:
    """track.suggest_failed (B→E) — 엔진이 다른 후보로 다시 제안한다."""
    try:
        async with asyncio.timeout(3):
            await redis.xadd(f'engine:{station_id}:in', {
                'type': 'track.suggest_failed', 'contract_version': '1', 'event_id': 'evt_' + uuid4().hex,
                'station_id': station_id, 'at': str(time.time()),
                'payload': json.dumps({'suggest_id': suggest_id, 'reason': reason})})
    except (RedisError, TimeoutError):
        log.warning('Could not report DJ pick failure %s (%s)', suggest_id, reason, exc_info=True)
    return reason
