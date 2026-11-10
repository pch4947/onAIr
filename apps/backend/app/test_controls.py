"""테스트 화면 전용 — 어드민만. 방송을 끊지 않고 코너를 바꾸고, 엔진의 편성 결정과 판단 흐름(이벤트)을 보여준다."""
import asyncio
import json
import time
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import RedisError

from .auth import get_current_user
from .station_runtime import station_runtime
from .stations import engine_call

router = APIRouter(prefix='/api')
Admin = Annotated[dict, Depends(get_current_user)]


class CornerSkipInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # 형식 파일의 코너 키 (topic_talk, text_corner, music_intro 등). 있는 코너인지는 엔진이 판단한다
    corner: str = Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')


def admin_station(user, request, station_id):
    if user.get('kind') != 'admin':
        raise HTTPException(403, 'Admin only')
    return station_runtime(request, station_id)


def discardable_segments(publisher, ids, records):
    """아직 송출기가 손대지 않은 멘트만 고른다. 재생·인코딩 중인 조각과 음악은 그대로 둔다."""
    covered = {segment['id'] for _, segment in publisher.finishing}
    covered |= {publisher.active_id, (publisher.current or {}).get('id')}
    chosen = []
    for key in ids:
        raw = records.get(key)
        if not raw or key in covered or json.loads(raw)['kind'] == 'music':
            continue
        if publisher.journal is not None and publisher.journal.status(key) is not None:
            continue  # MIXING·PUBLISHED 등 이미 송출기에 들어간 조각
        chosen.append(key)
    return chosen


def event_fields(kind, station_id, payload):
    return {'type': kind, 'contract_version': '1', 'event_id': 'evt_' + uuid4().hex,
            'station_id': station_id, 'at': str(time.time()), 'payload': json.dumps(payload)}


@router.post('/stations/{station_id}/corner-skip', status_code=202)
async def skip_corner(station_id: str, body: CornerSkipInput, request: Request, user: Admin):
    """지금 코너를 끝내고 고른 코너로 넘어가게 엔진에 요청한다. 202는 요청 접수이지 전환 완료가 아니다."""
    state = admin_station(user, request, station_id)
    if not state.settings.hls_enabled:
        raise HTTPException(503, 'HLS is disabled')
    prefix = f'backend:{station_id}'
    try:
        async with asyncio.timeout(3):
            raw = await state.redis.get(prefix + ':station')
            if not raw or json.loads(raw)['status'] != 'started':
                raise HTTPException(409, 'Station must be started')
            async with state.redis.pipeline(transaction=True) as pipe:
                pipe.lrange(prefix + ':queue', 0, -1)
                pipe.hgetall(prefix + ':segments')
                ids, records = await pipe.execute()
            discarded = discardable_segments(state.hls, ids, records)
            inbox = f'engine:{station_id}:in'
            async with state.redis.pipeline(transaction=True) as pipe:
                for key in discarded:
                    pipe.lrem(prefix + ':queue', 0, key)
                if discarded:
                    # 엔진이 버려진 세그먼트의 입력(인용 채팅·사연)을 되돌리고 버퍼 계산에서 빼게 한다
                    pipe.xadd(inbox, event_fields('segment.discarded', station_id, {'segment_ids': discarded}))
                pipe.xadd(inbox, event_fields('corner.skip', station_id, {'corner': body.corner}))
                await pipe.execute()
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    return {'corner': body.corner, 'discarded': len(discarded)}


# 엔진 결정 로그(계약 6.1절)는 초당 1~2건이고 대부분 "wait"다. 화면에는 무엇을 만들었는지만 보낸다
SHOWN_ACTIONS = {'generate', 'glue', 'no_material', 'tts_blocked', 'idle'}
DECISION_PAGE = 500
DECISION_PAGES_PER_CALL = 20


def public_input(item):
    # requester_ref(내부 사용자 id)는 화면에 내리지 않는다
    return {key: item.get(key) for key in ('request_id', 'kind', 'body', 'chat_type', 'rule', 'state', 'nickname')}


def public_decision(entry):
    context, decision = entry['context'], entry['decision']
    pending = {item['request_id']: item for item in context.get('pending_requests') or []}
    return {
        'id': entry['id'], 'at': entry['at'], 'action': decision['action'],
        'corner': decision.get('corner_type') or context.get('corner'), 'step': decision.get('step'),
        'situation': decision.get('situation'), 'criterion': decision.get('criterion'),
        'quotes': [public_input(pending.get(key) or {'request_id': key}) for key in decision.get('quotes') or []],
        'pending': [public_input(item) for item in pending.values()],
    }


async def read_engine_log(request, user, station_id, after, log, keep, shape):
    """엔진 로그(계약 6.1절)에서 이번 방송 것만 읽는다. after 없이 부르면 이 방송 시작부터, next를 after로 넘기면 그 뒤부터."""
    state = admin_station(user, request, station_id)
    try:
        async with asyncio.timeout(3):
            raw = await state.redis.get(f'backend:{station_id}:station')
    except (RedisError, TimeoutError):
        raise HTTPException(503, 'Redis unavailable') from None
    if not raw:
        return [], after
    since = json.loads(raw)['created_at']
    # 엔진 로그는 같은 station_id의 지난 방송까지 쌓여 있다. 처음 읽은 위치를 기억해 매번 앞에서부터 넘기지 않는다
    cursors = getattr(state, 'engine_log_cursors', None)
    if cursors is None:
        cursors = state.engine_log_cursors = {}
    cursor = after
    if cursor is None:
        cached = cursors.get(log)
        cursor = cached[1] if cached and cached[0] == since else None
    shown = []
    for _ in range(DECISION_PAGES_PER_CALL):
        params = {'limit': DECISION_PAGE} | ({'after': cursor} if cursor is not None else {})
        page = await engine_call(request, 'GET', f'/v1/stations/{station_id}/{log}', params=params)
        entries = page.get(log) or []
        shown += [shape(entry) for entry in entries if entry['at'] >= since and keep(entry)]
        older = [entry['id'] for entry in entries if entry['at'] < since]
        if after is None and older:  # 지난 방송의 마지막 위치 — 한 페이지에 이번 방송이 섞여 있어도 기억한다
            cursors[log] = (since, older[-1])
        if entries:
            cursor = entries[-1]['id']
        if page.get('next') is None or len(entries) < DECISION_PAGE:
            break
    return shown, cursor


@router.get('/stations/{station_id}/decisions')
async def list_decisions(station_id: str, request: Request, user: Admin,
                         after: Annotated[int | None, Query(ge=0)] = None):
    """지금 방송의 편성 결정 (무엇을 만들었는지만)."""
    shown, cursor = await read_engine_log(request, user, station_id, after, 'decisions',
                                          lambda entry: entry['decision']['action'] in SHOWN_ACTIONS, public_decision)
    return {'decisions': shown, 'next': cursor}


def public_event(entry):
    # 엔진이 패널용으로 만든 기록이라 내부 사용자 ID는 처음부터 없다 (#208)
    return {key: entry.get(key) for key in ('id', 'at', 'kind', 'summary', 'data')}


@router.get('/stations/{station_id}/events')
async def list_events(station_id: str, request: Request, user: Admin,
                      after: Annotated[int | None, Query(ge=0)] = None):
    """지금 방송의 엔진 판단 흐름 — 입력 라우팅, 채팅 선별, 생성 단계, 코너, 화제 전환, 송출 등."""
    shown, cursor = await read_engine_log(request, user, station_id, after, 'events', lambda entry: True, public_event)
    return {'events': shown, 'next': cursor}
