"""Multi-station step 2: channel list, one runtime per channel, shared engine status."""
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
from redis.asyncio import Redis

from app.config import Settings, load_settings, parse_station_ids
from app.main import create_app
from app.redis_connection import redis_lifespan
from app.station_lifecycle import EngineStatusCache
from app.station_runtime import default_runtime, ensure_runtimes


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


if __name__ == '__main__':
    unittest.main()
