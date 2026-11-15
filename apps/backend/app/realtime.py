"""방송 실시간 알림 웹소켓 (#312).

청취 화면이 방송 상태·채팅·대기열·투표를 각각 폴링하던 것을, 바뀐 순간 알려 주는 방식으로 바꾼다.

- 방송마다 감시 작업(StationHub) 하나가 접속자가 있는 동안만 돈다. 0.5초마다, 또는 요청 접수·엔진 이벤트
  처리 직후 깨워질 때(station.notify) 지금 상태를 읽어 직전과 비교한다. 바뀌는 곳이 여러 군데라
  보내는 코드를 곳곳에 넣는 대신 한곳에서 비교해 빠뜨리는 변경이 없게 했다.
- 채팅/요청은 바뀐 항목을 데이터째 보낸다({"type": "requests"}). 내 것 여부(mine)는 접속 때 받은 토큰으로
  소켓마다 정하고, 요청자 ID는 보내지 않는다.
- 방송 상태·대기열·투표는 바뀐 항목 이름만 알린다({"type": "changed"}). 프론트가 기존 API로 다시 받는다 —
  내가 고른 투표처럼 사용자별 값은 기존 API에 있다.
"""
import asyncio
import contextlib
import json
import logging
import time

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.exceptions import RedisError

from .api import public_request
from .auth import JWT_ALGORITHM
from .station_runtime import ensure_runtimes
from .votes import close_due, current_vote

log = logging.getLogger(__name__)
router = APIRouter()

TICK_SECONDS = 0.5
PING_SECONDS = 20
AUTH_WAIT_SECONDS = 5
SEND_TIMEOUT_SECONDS = 2
ALL_TOPICS = ('broadcast', 'requests', 'queue', 'vote')


class StationHub:
    """방송 하나의 접속자와 감시 작업."""

    def __init__(self, station):
        self.station = station
        self.clients: dict[WebSocket, str] = {}  # 소켓 → 사용자 ID
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.last: dict | None = None
        # 막 들어온 소켓. 다음 비교 기준을 읽은 뒤 전체를 다시 받으라고 알린다 —
        # 접속 전에 받아 둔 데이터와 비교 기준 사이에 바뀐 것을 놓치지 않게
        self.joining: set[WebSocket] = set()
        self.failing = False  # 읽기 실패 로그를 실패가 시작될 때 한 번만 남긴다

    def poke(self):
        self.wake.set()

    def join(self, websocket, user_id):
        self.clients[websocket] = user_id
        self.joining.add(websocket)
        self.poke()
        if self.task is None or self.task.done():
            self.last = None
            self.task = asyncio.create_task(self.run())

    def leave(self, websocket):
        self.clients.pop(websocket, None)
        self.joining.discard(websocket)
        if not self.clients and self.task is not None:
            self.task.cancel()
            self.task, self.last = None, None

    async def run(self):
        next_ping = time.monotonic() + PING_SECONDS
        while self.clients:
            # 비교 중에 들어온 깨우기를 놓치지 않게 읽기 전에 지운다
            self.wake.clear()
            # 한 번 실패해도 작업이 죽지 않게 한다 — 죽으면 화면은 "연결됨"인데 갱신이 멈춘다
            try:
                await self.tick()
                if self.failing:
                    log.info('Realtime snapshot recovered for %s', self.station.station_id)
                self.failing = False
            except Exception as error:
                if not self.failing:
                    if isinstance(error, (RedisError, TimeoutError, OSError)):
                        log.warning('Realtime snapshot failed for %s; retrying', self.station.station_id)
                    else:
                        log.exception('Realtime snapshot failed for %s; retrying', self.station.station_id)
                self.failing = True
            if time.monotonic() >= next_ping:
                await self.send_all(lambda _: {'type': 'ping', 'serverTime': time.time()})
                next_ping = time.monotonic() + PING_SECONDS
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(TICK_SECONDS):
                    await self.wake.wait()

    async def tick(self):
        current = await self.snapshot()
        joined, self.joining = self.joining, set()
        try:
            if self.last is not None:
                await self.publish(self.last, current, skip=joined)
        finally:
            self.last = current
            if joined:
                await self.send_all(lambda _: {'type': 'changed', 'topics': list(ALL_TOPICS)}, only=joined)

    async def snapshot(self) -> dict:
        """화면이 보는 값만 모은다. 버퍼 길이처럼 계속 바뀌는 값은 넣지 않는다."""
        station, redis, sid = self.station, self.station.redis, self.station.station_id
        prefix = f'backend:{sid}'
        now = time.time()
        async with asyncio.timeout(3):
            # GET /votes/current와 같이, 창이 지난 투표는 여기서도 닫는다
            await close_due(redis, sid, now)
            requests = await station.requests.list()
            async with redis.pipeline(transaction=False) as pipe:
                pipe.get(prefix + ':station')
                pipe.get(prefix + ':current-corner')
                pipe.lrange(prefix + ':queue', 0, 199)
                pipe.hget(prefix + ':track-plan', 'playing')
                record, corner, queue_ids, playing = await pipe.execute()
            submissions = await redis.hmget(prefix + ':music-submissions', queue_ids) if queue_ids else []
            vote = await current_vote(redis, sid, now)
            viewers = await station.presence.count(now)
        record = json.loads(record) if record else {}
        config = record.get('config') or {}
        hls = station.hls
        vote_key = (vote['vote_id'], vote['status']) if vote else None
        return {
            'requests': {entry['id']: entry for entry in requests},
            'broadcast': (record.get('status'), config.get('format'), (config.get('persona') or {}).get('dj_name'),
                          corner, vote_key, viewers,
                          getattr(hls, 'ready', None), getattr(hls, 'session', None),
                          getattr(hls, 'error', None) or getattr(hls, 'redis_error', None)),
            'queue': (tuple(raw for raw in submissions if raw), playing),
            'vote': vote_key,
        }

    async def publish(self, before: dict, after: dict, skip=frozenset()):
        topics = [topic for topic in ('broadcast', 'queue', 'vote') if before[topic] != after[topic]]
        changed = [entry for request_id, entry in after['requests'].items()
                   if before['requests'].get(request_id) != entry]
        # 새 방송이 시작되면 지난 방송 요청이 목록에서 빠진다 — 빠진 것은 목록을 다시 받게 한다
        if before['requests'].keys() - after['requests'].keys():
            topics.append('requests')
        elif changed:
            await self.send_all(lambda user_id: {
                'type': 'requests', 'requests': [public_request(entry, {'id': user_id}) for entry in changed]},
                skip=skip)
        if topics:
            await self.send_all(lambda _: {'type': 'changed', 'topics': topics}, skip=skip)

    async def send_all(self, message_for, only=None, skip=frozenset()):
        async def send(websocket, user_id):
            try:
                async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                    await websocket.send_json(message_for(user_id))
            except Exception:
                # 끊긴 소켓 — 받는 쪽 루프가 정리하지만, 그 전에 다시 보내지 않게 뺀다
                self.clients.pop(websocket, None)
                with contextlib.suppress(Exception):
                    await websocket.close()
        await asyncio.gather(*(send(ws, uid) for ws, uid in list(self.clients.items())
                               if ws not in skip and (only is None or ws in only)))


def hub_for(station) -> StationHub:
    if station.realtime is None:
        station.realtime = StationHub(station)
    return station.realtime


# 닫는 코드 — 프론트는 이 둘이면 다시 붙지 않는다
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404


def token_from(message) -> str | None:
    """첫 메시지 {"type": "auth", "token": ...}. 토큰을 주소(쿼리)에 넣으면 접근 로그에 남아서 메시지로 받는다."""
    if not isinstance(message, dict) or message.get('type') != 'auth':
        return None
    token = message.get('token')
    return token if isinstance(token, str) and token else None


async def user_id_from(app, message) -> str | None:
    """REST(get_current_user)와 같은 기준 — 서명·만료가 맞고 계정이 아직 있어야 한다(로그아웃한 게스트는 지워진다).
    채팅 내용은 REST에서도 로그인해야 보이므로 토큰이 없거나 틀리면 받지 않는다."""
    token = token_from(message)
    if token is None:
        return None
    try:
        payload = jwt.decode(token, app.state.settings.jwt_secret, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if not payload.get('sub'):
        return None
    async with asyncio.timeout(3):
        user = await app.state.users.get_by_email(payload['sub'])
    return user['id'] if user else None


async def receive_json_or_none(websocket):
    message = await websocket.receive()
    if message['type'] == 'websocket.disconnect':
        raise WebSocketDisconnect(message.get('code', 1000))
    try:
        return json.loads(message.get('text') or '')
    except ValueError:
        return None


@router.websocket('/ws/stations/{station_id}')
async def station_socket(websocket: WebSocket, station_id: str):
    # 수락 전에 닫으면 브라우저는 닫는 코드를 못 받고(HTTP 403) 계속 다시 붙는다 — 수락한 뒤 코드를 실어 닫는다
    await websocket.accept()
    station = ensure_runtimes(websocket.app.state).get(station_id)
    if station is None:
        await websocket.close(code=CLOSE_NOT_FOUND)
        return
    hub = hub_for(station)
    try:
        try:
            async with asyncio.timeout(AUTH_WAIT_SECONDS):
                first = await receive_json_or_none(websocket)
            user_id = await user_id_from(websocket.app, first)
        except (TimeoutError, RedisError):
            user_id = None
        if user_id is None:
            await websocket.close(code=CLOSE_UNAUTHORIZED)
            return
        await websocket.send_json({'type': 'hello', 'serverTime': time.time()})
        hub.join(websocket, user_id)
        # 보내기는 감시 작업이 한다. 여기서는 끊길 때까지 받기만 하고 내용은 버린다(텍스트·바이너리 모두)
        while (await websocket.receive())['type'] != 'websocket.disconnect':
            pass
    except WebSocketDisconnect:
        pass
    finally:
        hub.leave(websocket)
