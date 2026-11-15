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


class TokenTests(unittest.TestCase):
    def test_auth_message_gives_user_id_and_anything_else_is_anonymous(self):
        token = create_access_token({'email': 'a@example.com', 'id': 'u1'}, 'secret')
        self.assertEqual(user_id_from({'type': 'auth', 'token': token}, 'secret'), 'u1')
        self.assertIsNone(user_id_from({'type': 'auth', 'token': token}, 'other-secret'))
        self.assertIsNone(user_id_from({'type': 'auth', 'token': None}, 'secret'))
        self.assertIsNone(user_id_from(None, 'secret'))


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(Settings('127.0.0.1', 3000, station_id='st_ws', jwt_secret='secret'))
        self.client = TestClient(self.app)
        station = ensure_runtimes(self.app.state)['st_ws']
        self.hub = hub_for(station)
        self.current = state([entry('r1', listener='u1')])

        async def snapshot():
            return json.loads(json.dumps(self.current))

        self.hub.snapshot = snapshot

    def test_unknown_station_is_closed(self):
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect) as caught:
            with self.client.websocket_connect('/ws/stations/nope') as socket:
                socket.receive_json()
        self.assertEqual(caught.exception.code, 4404)

    def test_hello_then_full_refetch_then_live_request(self):
        token = create_access_token({'email': 'a@example.com', 'id': 'u1'}, 'secret')
        with self.client.websocket_connect('/ws/stations/st_ws') as socket:
            socket.send_json({'type': 'auth', 'token': token})
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
