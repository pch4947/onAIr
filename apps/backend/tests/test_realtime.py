"""방송 실시간 알림 웹소켓 (#312)."""
import asyncio
import json
import os
import time
import unittest
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import realtime
from app.api import PresenceStore
from app.auth import create_access_token
from app.config import Settings
from app.main import create_app
from app.realtime import ALL_TOPICS, StationHub, hub_for, user_id_from
from app.requests import RequestStore
from app.station_runtime import ensure_runtimes


def entry(request_id, status='requested', listener='u1'):
    return {'id': request_id, 'prompt': request_id, 'listenerId': listener, 'listenerName': None,
            'kind': 'story', 'status': status, 'createdAt': '2026-11-15T00:00:00.000+00:00'}


def state(requests=(), broadcast=('started',), queue=((), None), vote=None):
    return {'requests': {item['id']: item for item in requests}, 'broadcast': broadcast, 'queue': queue, 'vote': vote}


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, message):
        self.sent.append(message)

    async def close(self):
        pass

    def take(self):
        sent, self.sent = self.sent, []
        return sent


class HubTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.current = state()
        self.hub = StationHub(SimpleNamespace(station_id='st_rt'))
        self.reads = 0

        async def snapshot():
            self.reads += 1
            return json.loads(json.dumps(self.current))  # 매번 새 객체 — 실제처럼 비교가 값으로 되는지 본다

        self.hub.snapshot = snapshot

    async def asyncTearDown(self):
        for socket in list(self.hub.clients):
            self.hub.leave(socket)

    async def settle(self):
        # 깨우기를 처리할 만큼만 기다린다 (주기 0.5초보다 짧게)
        await asyncio.sleep(0.05)

    async def joined(self, user_id='u1'):
        socket = FakeSocket()
        self.hub.join(socket, user_id)
        await self.settle()
        return socket

    async def test_new_socket_is_told_to_refetch_everything_once(self):
        socket = await self.joined()
        self.assertEqual(socket.take(), [{'type': 'changed', 'topics': list(ALL_TOPICS)}])
        self.hub.poke()
        await self.settle()
        self.assertEqual(socket.take(), [])

    async def test_request_change_is_sent_with_data_and_per_socket_mine(self):
        mine, other = await self.joined('u1'), await self.joined('u2')
        mine.take(), other.take()
        self.current = state([entry('r1', 'requested', listener='u1')])
        self.hub.poke()
        await self.settle()
        sent = mine.take()
        self.assertEqual(sent[0]['type'], 'requests')
        self.assertEqual(sent[0]['requests'][0]['id'], 'r1')
        self.assertTrue(sent[0]['requests'][0]['mine'])
        self.assertNotIn('listenerId', sent[0]['requests'][0])
        self.assertFalse(other.take()[0]['requests'][0]['mine'])
        # 상태만 바뀌어도 그 항목만 다시 온다
        self.current = state([entry('r1', 'reflected', listener='u1')])
        self.hub.poke()
        await self.settle()
        self.assertEqual([item['status'] for item in mine.take()[0]['requests']], ['reflected'])

    async def test_other_topics_send_names_only_when_changed(self):
        socket = await self.joined()
        socket.take()
        self.current = state(broadcast=('stopping',), vote=['v1', 'open'])
        self.hub.poke()
        await self.settle()
        self.assertEqual(socket.take(), [{'type': 'changed', 'topics': ['broadcast', 'vote']}])
        self.hub.poke()
        await self.settle()
        self.assertEqual(socket.take(), [])

    async def test_requests_dropping_out_asks_for_a_refetch(self):
        self.current = state([entry('old')])
        socket = await self.joined()
        socket.take()
        self.current = state()  # 새 방송이 시작돼 지난 방송 요청이 빠졌다
        self.hub.poke()
        await self.settle()
        self.assertEqual(socket.take(), [{'type': 'changed', 'topics': ['requests']}])

    async def test_without_poke_changes_arrive_on_the_next_tick(self):
        socket = await self.joined()
        socket.take()
        self.current = state(queue=[['song'], 'seg1'])
        await asyncio.sleep(realtime.TICK_SECONDS + 0.2)
        self.assertEqual(socket.take(), [{'type': 'changed', 'topics': ['queue']}])

    async def test_watcher_stops_when_last_socket_leaves(self):
        first, second = await self.joined(), await self.joined()
        task = self.hub.task
        self.hub.leave(first)
        self.assertIs(self.hub.task, task)
        self.hub.leave(second)
        await asyncio.sleep(0)
        self.assertIsNone(self.hub.task)
        self.assertTrue(task.cancelled() or task.done())
        reads = self.reads
        await asyncio.sleep(realtime.TICK_SECONDS + 0.1)
        self.assertEqual(self.reads, reads)

    async def test_broken_socket_is_dropped(self):
        socket = await self.joined()

        async def broken(message):
            raise RuntimeError('closed')

        socket.send_json = broken
        self.current = state(broadcast=('ended',))
        self.hub.poke()
        await self.settle()
        self.assertNotIn(socket, self.hub.clients)

    async def test_watcher_survives_a_failing_publish(self):
        socket = await self.joined()
        socket.take()
        calls = []
        original = self.hub.publish

        async def flaky(before, after, skip=frozenset()):
            calls.append(1)
            if len(calls) == 1:
                raise KeyError('broken entry')
            await original(before, after, skip)

        self.hub.publish = flaky
        self.current = state(broadcast=('stopping',))
        self.hub.poke()
        await self.settle()
        self.assertFalse(self.hub.task.done())
        # 실패한 회차의 값도 기준이 됐으므로 다음 변경부터 정상으로 알린다
        self.current = state(broadcast=('ended',))
        self.hub.poke()
        await self.settle()
        self.assertEqual(socket.take(), [{'type': 'changed', 'topics': ['broadcast']}])

    async def test_snapshot_failure_logs_once_and_recovers(self):
        failing = True
        good = self.hub.snapshot

        async def snapshot():
            if failing:
                raise ConnectionError('redis down')
            return await good()

        self.hub.snapshot = snapshot
        with self.assertLogs('app.realtime', level='INFO') as logs:
            socket = await self.joined()
            for _ in range(3):
                self.hub.poke()
                await self.settle()
            self.assertEqual(socket.take(), [])  # 읽기 전에는 기준이 없어 아무것도 안 보낸다
            failing = False
            self.hub.poke()
            await self.settle()
        warnings = [line for line in logs.output if 'failed' in line]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(any('recovered' in line for line in logs.output))
        # 복구 뒤 막 들어온 소켓은 전체를 다시 받으라는 알림을 받는다
        self.assertEqual(socket.take(), [{'type': 'changed', 'topics': list(ALL_TOPICS)}])

    async def test_stations_do_not_share_messages(self):
        other_hub = StationHub(SimpleNamespace(station_id='st_other'))
        other_current = state()

        async def other_snapshot():
            return json.loads(json.dumps(other_current))

        other_hub.snapshot = other_snapshot
        here, there = await self.joined(), FakeSocket()
        other_hub.join(there, 'u1')
        await self.settle()
        here.take(), there.take()
        self.current = state([entry('only-here')])
        self.hub.poke()
        await self.settle()
        self.assertEqual(len(here.take()), 1)
        self.assertEqual(there.take(), [])
        other_hub.leave(there)


class FakeUsers:
    def __init__(self, *users):
        self.users = {user['email']: user for user in users}

    async def get_by_email(self, email):
        return self.users.get(email)


USER = {'email': 'a@example.com', 'id': 'u1'}


class TokenTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_rules_as_rest_login(self):
        app = SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(jwt_secret='secret'), users=FakeUsers(USER)))
        token = create_access_token(USER, 'secret')
        self.assertEqual(await user_id_from(app, {'type': 'auth', 'token': token}), 'u1')
        # 서명이 다르거나, 토큰이 없거나, 형식이 다르거나, 계정이 지워졌으면(로그아웃한 게스트) 거절
        self.assertIsNone(await user_id_from(app, {'type': 'auth', 'token': create_access_token(USER, 'other')}))
        self.assertIsNone(await user_id_from(app, {'type': 'auth', 'token': None}))
        self.assertIsNone(await user_id_from(app, {'type': 'auth', 'token': 123}))
        self.assertIsNone(await user_id_from(app, {'type': 'hello', 'token': token}))
        self.assertIsNone(await user_id_from(app, ['auth', token]))
        self.assertIsNone(await user_id_from(app, None))
        gone = create_access_token({'email': 'gone@example.com', 'id': 'u9'}, 'secret')
        self.assertIsNone(await user_id_from(app, {'type': 'auth', 'token': gone}))


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(Settings('127.0.0.1', 3000, station_id='st_ws', jwt_secret='secret'))
        self.app.state.users = FakeUsers(USER)
        self.client = TestClient(self.app)
        station = ensure_runtimes(self.app.state)['st_ws']
        self.hub = hub_for(station)
        self.current = state([entry('r1', listener='u1')])
        self.token = create_access_token(USER, 'secret')

        async def snapshot():
            return json.loads(json.dumps(self.current))

        self.hub.snapshot = snapshot

    def closed_with(self, path, first=None, raw=None):
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect) as caught:
            with self.client.websocket_connect(path) as socket:
                if raw is not None:
                    socket.send_text(raw)
                elif first is not None:
                    socket.send_json(first)
                socket.receive_json()
        return caught.exception.code

    def test_unknown_station_is_closed_with_a_code_the_browser_sees(self):
        self.assertEqual(self.closed_with('/ws/stations/nope'), 4404)

    def test_missing_or_bad_token_is_closed_4401(self):
        self.assertEqual(self.closed_with('/ws/stations/st_ws', {'type': 'auth', 'token': None}), 4401)
        self.assertEqual(self.closed_with('/ws/stations/st_ws', {'type': 'auth', 'token': 'nope'}), 4401)
        self.assertEqual(self.closed_with('/ws/stations/st_ws', raw='not json'), 4401)
        self.assertEqual(self.hub.clients, {})

    def test_hello_then_full_refetch_then_live_request(self):
        with self.client.websocket_connect('/ws/stations/st_ws') as socket:
            socket.send_json({'type': 'auth', 'token': self.token})
            hello = socket.receive_json()
            self.assertEqual(hello['type'], 'hello')
            self.assertAlmostEqual(hello['serverTime'], time.time(), delta=5)
            self.assertEqual(socket.receive_json(), {'type': 'changed', 'topics': list(ALL_TOPICS)})
            self.current = state([entry('r1', 'selected', listener='u1')])
            ensure_runtimes(self.app.state)['st_ws'].notify()
            message = socket.receive_json()
            self.assertEqual(message['type'], 'requests')
            self.assertEqual((message['requests'][0]['status'], message['requests'][0]['mine']), ('selected', True))
        self.assertEqual(self.hub.clients, {})

    def test_client_messages_after_auth_are_ignored(self):
        with self.client.websocket_connect('/ws/stations/st_ws') as socket:
            socket.send_json({'type': 'auth', 'token': self.token})
            socket.receive_json(), socket.receive_json()
            socket.send_text('garbage')
            socket.send_bytes(b'\x00\x01')
            socket.send_json({'type': 'auth', 'token': 'other'})
            self.current = state([entry('r1', 'reflected', listener='u1')])
            ensure_runtimes(self.app.state)['st_ws'].notify()
            self.assertEqual(socket.receive_json()['requests'][0]['status'], 'reflected')


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    """실제 Redis 키를 읽어 화면이 보는 값이 바뀔 때만 다르게 나오는지."""

    async def asyncSetUp(self):
        from redis.asyncio import Redis
        self.sid = 'st_rt_' + uuid.uuid4().hex
        self.prefix = f'backend:{self.sid}'
        self.redis = Redis.from_url('redis://127.0.0.1:6379/15', decode_responses=True)
        station = SimpleNamespace(station_id=self.sid, redis=self.redis, hls=None,
                                  requests=RequestStore(self.redis, self.sid), presence=PresenceStore(self.redis, self.sid))
        self.hub = StationHub(station)
        await self.redis.set(self.prefix + ':station', json.dumps({
            'status': 'started', 'created_at': time.time() - 60, 'config': {'format': 'talk', 'persona': {'dj_name': '고요'}}}))
        created = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        await self.redis.hset(self.prefix + ':requests', 'r1', json.dumps({**entry('r1'), 'createdAt': created}))

    async def asyncTearDown(self):
        keys = [key async for key in self.redis.scan_iter(match=f'{self.prefix}:*')]
        if keys:
            await self.redis.delete(*keys)
        await self.redis.aclose()

    async def test_snapshot_follows_screen_values(self):
        first = await self.hub.snapshot()
        self.assertEqual(first['requests']['r1']['status'], 'requested')
        self.assertEqual(first['broadcast'][:3], ('started', 'talk', '고요'))
        self.assertEqual(await self.hub.snapshot(), first)

        await self.redis.hset(self.prefix + ':request-states', 'r1', json.dumps({'state': 'selected'}))
        await self.redis.set(self.prefix + ':current-corner', json.dumps({'corner': 'story_corner'}))
        await self.redis.zadd(self.prefix + ':presence', {'c1': time.time()})
        second = await self.hub.snapshot()
        self.assertEqual(second['requests']['r1']['status'], 'selected')
        self.assertNotEqual(second['broadcast'], first['broadcast'])
        self.assertEqual(second['queue'], first['queue'])
