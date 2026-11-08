"""Multi-station: channel runtimes (step 2), channel choice and station list (step 3), station paths (step 4)."""
import asyncio
from dataclasses import replace
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import httpx
from fastapi import HTTPException, Request
from redis.asyncio import Redis

from app.api import PresenceStore
from app.auth import get_current_user
from app.config import Settings, load_settings, parse_station_ids
from app.main import create_app
from app.redis_connection import redis_lifespan
from app.requests import RequestStore
from app.station_lifecycle import EngineStatusCache
from app.station_runtime import default_runtime, ensure_runtimes
from app.stations import StationStore, choose_channel
from test_stations import BODY


def channel_settings(*ids, **extra):
    return replace(Settings('127.0.0.1', 3000, **extra), station_id=ids[0], station_ids=tuple(ids))


class ChannelConfigTests(unittest.TestCase):
    def test_parses_channel_list(self):
        self.assertEqual(parse_station_ids(' st_ch01, st_ch02 ,'), ('st_ch01', 'st_ch02'))
        self.assertEqual(parse_station_ids(''), ())
        for raw in ['st_ch01,st_ch01', 'st bad', 'x' * 65]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_station_ids(raw)

    def test_channel_list_wins_and_its_first_entry_is_the_default(self):
        with patch('app.config.load_dotenv'), patch.dict(os.environ, {
                'ONAIR_STATION_ID': 'st_old', 'ONAIR_STATION_IDS': 'st_ch01,st_ch02'}):
            settings = load_settings()
        self.assertEqual((settings.station_id, settings.channel_ids), ('st_ch01', ('st_ch01', 'st_ch02')))
        with patch('app.config.load_dotenv'), patch.dict(os.environ, {'ONAIR_STATION_ID': 'st_old'}):
            os.environ.pop('ONAIR_STATION_IDS', None)
            settings = load_settings()
        self.assertEqual((settings.station_id, settings.channel_ids), ('st_old', ('st_old',)))

    def test_each_channel_gets_its_own_settings_copy(self):
        state = SimpleNamespace(settings=channel_settings('st_ch01', 'st_ch02', hls_enabled=True))
        runtimes = ensure_runtimes(state)
        self.assertEqual(list(runtimes), ['st_ch01', 'st_ch02'])
        self.assertEqual(state.default_station_id, 'st_ch01')
        self.assertIs(runtimes['st_ch01'].settings, state.settings)
        second = runtimes['st_ch02'].settings
        self.assertEqual(second.station_id, 'st_ch02')
        self.assertEqual(replace(second, station_id='st_ch01'), state.settings)


class ChannelRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_station_paths_accept_every_configured_channel(self):
        app = create_app(channel_settings('st_ch01', 'st_ch02'))
        for station in ensure_runtimes(app.state).values():
            station.presence = SimpleNamespace(heartbeat=AsyncMock())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            for sid, status in (('st_ch01', 204), ('st_ch02', 204), ('st_ch03', 404)):
                with self.subTest(sid=sid):
                    response = await client.post(f'/api/stations/{sid}/presence', json={'clientId': 'c1'})
                    self.assertEqual(response.status_code, status)
        app.state.runtimes['st_ch02'].presence.heartbeat.assert_awaited_once()
        self.assertIs(default_runtime(app), app.state.runtimes['st_ch01'])


class EngineStatusCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_channels_share_one_engine_status_call(self):
        calls = []

        def reply(request):
            calls.append(request)
            if len(calls) == 2:
                return httpx.Response(503)
            return httpx.Response(200, json={'stations': [{'station_id': 'st_ch02'}]})
        http = httpx.AsyncClient(transport=httpx.MockTransport(reply), base_url='http://engine')
        cache = EngineStatusCache(http, ttl=60)
        results = await asyncio.gather(*(cache.station_ids() for _ in range(5)))
        self.assertEqual(results, [{'st_ch02'}] * 5)
        self.assertEqual(len(calls), 1)
        cache.ttl = -1  # always stale (Windows monotonic ticks are ~15 ms)
        with self.assertRaises(httpx.HTTPStatusError):
            await cache.station_ids()
        self.assertEqual(await cache.station_ids(), {'st_ch02'})  # a failure is not remembered
        self.assertEqual(len(calls), 3)
        await http.aclose()


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class ChannelRedisTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_channels_consume_their_own_engine_streams(self):
        ids = ['st_chan_' + uuid.uuid4().hex[:10] for _ in range(2)]
        redis = Redis.from_url('redis://127.0.0.1:6379/0', decode_responses=True)
        app = SimpleNamespace(state=SimpleNamespace(settings=channel_settings(
            *ids, redis_url='redis://127.0.0.1:6379/0', hls_enabled=False)))
        try:
            async with redis_lifespan(app):
                consumers = [app.state.runtimes[sid].engine_consumer for sid in ids]
                self.assertEqual([c.stream for c in consumers], [f'engine:{sid}:out' for sid in ids])
                self.assertIs(app.state.runtimes[ids[0]].music.lock, app.state.runtimes[ids[1]].music.lock)
                await redis.xadd(f'engine:{ids[1]}:out', dict(
                    type='request.state', contract_version='1', station_id=ids[1], event_id='evt_' + uuid.uuid4().hex,
                    at=str(time.time()), payload=json.dumps(dict(request_id='req_two', state='screened'))))
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and not await redis.hget(
                        f'backend:{ids[1]}:request-states', 'req_two'):
                    await asyncio.sleep(0.1)
                self.assertEqual(json.loads(await redis.hget(
                    f'backend:{ids[1]}:request-states', 'req_two'))['state'], 'screened')
                self.assertIsNone(await redis.hget(f'backend:{ids[0]}:request-states', 'req_two'))
        finally:
            for sid in ids:
                keys = [k async for k in redis.scan_iter(f'backend:{sid}:*')]
                keys += [k async for k in redis.scan_iter(f'engine:{sid}:*')]
                if keys:
                    await redis.delete(*keys)
            await redis.aclose()


def record(sid, owner, status='started', retry_key='k', fingerprint='f'):
    return dict(station_id=sid, owner_id=owner, status=status, retry_key=retry_key, fingerprint=fingerprint)


class ChooseChannelTests(unittest.TestCase):
    def setUp(self):
        self.a, self.b = SimpleNamespace(station_id='st_a'), SimpleNamespace(station_id='st_b')

    def test_retry_returns_the_same_channel_even_after_it_ended(self):
        channels = [(self.a, record('st_a', 'u1', 'ended', 'retry', 'fp')), (self.b, None)]
        self.assertIs(choose_channel(channels, 'u1', 'retry', 'fp'), self.a)

    def test_one_live_broadcast_per_user(self):
        channels = [(self.a, record('st_a', 'u1', 'preparing')), (self.b, None)]
        with self.assertRaises(HTTPException) as caught:
            choose_channel(channels, 'u1', 'other-key', 'fp')
        self.assertEqual((caught.exception.status_code, caught.exception.detail['station_id']), (409, 'st_a'))

    def test_first_free_or_ended_channel_then_busy(self):
        self.assertIs(choose_channel([(self.a, record('st_a', 'u2')), (self.b, None)], 'u1', 'k1', 'fp'), self.b)
        self.assertIs(choose_channel([(self.a, record('st_a', 'u2', 'ended')), (self.b, None)], 'u1', 'k1', 'fp'),
                      self.a)
        with self.assertRaises(HTTPException) as caught:
            choose_channel([(self.a, record('st_a', 'u2')), (self.b, record('st_b', 'u3', 'rejected'))],
                           'u1', 'k1', 'fp')
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.headers['Retry-After'], '30')


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class ChannelCreationRedisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ids = ['st_pick_' + uuid.uuid4().hex[:10] for _ in range(2)]
        self.redis = Redis.from_url('redis://127.0.0.1:6379/0', decode_responses=True)
        self.app = create_app(channel_settings(*self.ids))
        self.app.state.redis = self.redis
        for sid, station in ensure_runtimes(self.app.state).items():
            station.stations = StationStore(self.redis, sid)
            station.presence = PresenceStore(self.redis, sid)
            station.hls = SimpleNamespace(ready=False)

        def user_from_header(request: Request):
            return {'id': request.headers['x-user'], 'kind': 'member', 'name': request.headers['x-user']}
        self.app.dependency_overrides[get_current_user] = user_from_header
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        for sid in self.ids:
            keys = [k async for k in self.redis.scan_iter(f'backend:{sid}:*')]
            if keys:
                await self.redis.delete(*keys)
        for entry_id, fields in await self.redis.xrange('engine:control'):
            if fields.get('station_id') in self.ids:
                await self.redis.xdel('engine:control', entry_id)
        await self.redis.aclose()

    async def create(self, user, key):
        return await self.client.post('/api/stations', json=BODY, headers={'x-user': user, 'Idempotency-Key': key})

    async def test_hosts_get_free_channels_one_each_until_full(self):
        first, second = await asyncio.gather(self.create('host-a', 'a1'), self.create('host-b', 'b1'))
        self.assertEqual((first.status_code, second.status_code), (202, 202))
        self.assertEqual({first.json()['station']['station_id'], second.json()['station']['station_id']},
                         set(self.ids))
        again = await self.create('host-a', 'a1')
        self.assertEqual(again.json()['station'], first.json()['station'])
        other = await self.create('host-a', 'a2')
        self.assertEqual(other.status_code, 409)
        self.assertEqual(other.json()['detail']['station_id'], first.json()['station']['station_id'])
        full = await self.create('host-c', 'c1')
        self.assertEqual((full.status_code, full.headers['retry-after']), (503, '30'))
        controls = [f for _, f in await self.redis.xrange('engine:control') if f.get('station_id') in self.ids]
        self.assertEqual(sorted(f['station_id'] for f in controls), sorted(self.ids))

    async def test_station_list_and_mine(self):
        created = (await self.create('host-a', 'a1')).json()['station']
        await self.redis.zadd(f"backend:{created['station_id']}:presence", {'listener': time.time()})
        listed = await self.client.get('/api/stations', headers={'x-user': 'listener'})
        self.assertEqual(listed.status_code, 200)
        [card] = listed.json()['stations']
        self.assertEqual({k: card[k] for k in ('station_id', 'status', 'dj_name', 'format', 'listeners', 'mine')},
                         {'station_id': created['station_id'], 'status': 'preparing', 'dj_name': '새벽',
                          'format': 'talk', 'listeners': 1, 'mine': False})
        self.assertNotIn('owner_id', listed.text)
        self.assertTrue((await self.client.get('/api/stations', headers={'x-user': 'host-a'})).json()
                        ['stations'][0]['mine'])
        mine = (await self.client.get('/api/stations/mine', headers={'x-user': 'host-a'})).json()
        self.assertEqual(mine['station']['station_id'], created['station_id'])
        self.assertIsNone((await self.client.get('/api/stations/mine', headers={'x-user': 'listener'})).json()
                          ['station'])
        raw = json.loads(await self.redis.get(f"backend:{created['station_id']}:station"))
        await self.redis.set(f"backend:{created['station_id']}:station", json.dumps(raw | {'status': 'ended'}))
        self.assertEqual((await self.client.get('/api/stations', headers={'x-user': 'host-a'})).json()['stations'],
                         [])


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class StationRouteRedisTests(unittest.IsolatedAsyncioTestCase):
    """Step 4: per-station request and state paths; station-less paths keep using the first channel."""

    async def asyncSetUp(self):
        self.ids = ['st_route_' + uuid.uuid4().hex[:10] for _ in range(2)]
        self.redis = Redis.from_url('redis://127.0.0.1:6379/0', decode_responses=True)
        self.app = create_app(channel_settings(*self.ids))
        self.app.state.redis = self.redis
        for sid, station in ensure_runtimes(self.app.state).items():
            station.requests = RequestStore(self.redis, sid)
            station.presence = PresenceStore(self.redis, sid)
            station.engine_consumer = SimpleNamespace(prefix=f'backend:{sid}')
            station.hls = SimpleNamespace(ready=False)
        self.app.dependency_overrides[get_current_user] = lambda: {'id': 'u1', 'name': '청취자'}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        for sid in self.ids:
            keys = [k async for k in self.redis.scan_iter(f'backend:{sid}:*')]
            keys += [k async for k in self.redis.scan_iter(f'engine:{sid}:*')]
            if keys:
                await self.redis.delete(*keys)
        await self.redis.aclose()

    async def inbox(self, sid):
        return [json.loads(f['payload'])['body'] for _, f in await self.redis.xrange(f'engine:{sid}:in')]

    async def test_requests_go_to_the_station_in_the_path(self):
        first, second = self.ids
        sent = await self.client.post(f'/api/stations/{second}/requests', json={'prompt': '둘째 채널', 'kind': 'chat'})
        self.assertEqual(sent.status_code, 202)
        legacy = await self.client.post('/api/requests', json={'prompt': '기본 채널', 'kind': 'chat'})
        self.assertEqual(legacy.status_code, 202)
        self.assertEqual((await self.inbox(first), await self.inbox(second)), (['기본 채널'], ['둘째 채널']))
        listed = (await self.client.get(f'/api/stations/{second}/requests')).json()['requests']
        self.assertEqual([(r['prompt'], r['mine']) for r in listed], [('둘째 채널', True)])
        self.assertEqual([r['prompt'] for r in (await self.client.get('/api/requests')).json()['requests']],
                         ['기본 채널'])
        request_id = sent.json()['request']['id']
        state = await self.client.get(f'/api/stations/{second}/requests/{request_id}/state')
        self.assertEqual(state.json(), {'request_id': request_id, 'state': 'requested'})
        self.assertEqual((await self.client.get(f'/api/requests/{request_id}/state')).status_code, 404)
        history = await self.client.get(f'/api/stations/{second}/requests/{request_id}/history')
        self.assertEqual([h['state'] for h in history.json()['history']], ['requested'])
        for path in ('/requests', '/stream/state', '/broadcast/queue', '/requests/x/state'):
            with self.subTest(path=path):
                self.assertEqual((await self.client.get(f'/api/stations/st_unknown{path}')).status_code, 404)

    async def test_stream_state_and_queue_describe_the_station_in_the_path(self):
        second = self.ids[1]
        await self.redis.set(f'backend:{second}:station', json.dumps(
            {'station_id': second, 'status': 'started', 'config': {'format': 'news'}}))
        await self.redis.zadd(f'backend:{second}:presence', {'listener': time.time()})
        state = (await self.client.get(f'/api/stations/{second}/stream/state')).json()
        self.assertEqual((state['stationId'], state['stationStatus'], state['stationFormat'], state['viewerCount']),
                         (second, 'started', 'news', 1))
        legacy = (await self.client.get('/api/stream/state')).json()
        self.assertEqual((legacy['stationId'], legacy['stationStatus'], legacy['viewerCount']), (self.ids[0], 'idle', 0))
        queue = (await self.client.get(f'/api/stations/{second}/broadcast/queue')).json()
        self.assertEqual((queue['station_id'], queue['count']), (second, 0))


if __name__ == '__main__':
    unittest.main()
