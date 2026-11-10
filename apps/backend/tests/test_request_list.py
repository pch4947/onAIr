"""요청 목록은 그 채널의 현재(또는 마지막) 방송 것만 — 같은 채널의 지난 방송 채팅이 섞이지 않는다 (#250)."""
from datetime import datetime, timezone
import json
import os
import unittest
import uuid

from redis.asyncio import Redis

from app.requests import RequestStore


def request(key, at, status='requested'):
    created = datetime.fromtimestamp(at, timezone.utc).isoformat(timespec='milliseconds')
    return json.dumps({'id': key, 'prompt': key, 'listenerId': 'u1', 'listenerName': None, 'kind': 'story',
                       'status': status, 'createdAt': created})


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class RequestListTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.station = 'st_req_' + uuid.uuid4().hex
        self.prefix = f'backend:{self.station}'
        self.redis = Redis.from_url('redis://127.0.0.1:6379/15', decode_responses=True)
        self.store = RequestStore(self.redis, self.station)
        now = datetime.now(timezone.utc).timestamp()
        self.started = now - 60
        # 지난 방송 채팅 2건(그중 하나는 아직 대기 상태로 남음)과 이번 방송 채팅 2건
        await self.redis.hset(self.prefix + ':requests', mapping={
            'old_done': request('old_done', now - 600, 'expired'), 'old_stuck': request('old_stuck', now - 500),
            'new_one': request('new_one', now - 30), 'new_two': request('new_two', now - 10)})

    async def asyncTearDown(self):
        keys = [k async for k in self.redis.scan_iter(match=f'{self.prefix}:*')]
        if keys:
            await self.redis.delete(*keys)
        await self.redis.aclose()

    async def set_station(self, status):
        await self.redis.set(self.prefix + ':station', json.dumps({'status': status, 'created_at': self.started}))

    async def test_only_current_broadcast(self):
        await self.set_station('started')
        self.assertEqual([entry['id'] for entry in await self.store.list()], ['new_one', 'new_two'])
        # 지난 방송에 남은 대기 요청이 대기 집계를 부풀리지 않는다
        metrics = await self.store.metrics()
        self.assertEqual(metrics['pendingRequestCount'], 2)
        self.assertLess(metrics['oldestRequestAgeSeconds'], 60)

    async def test_ended_broadcast_keeps_its_own_requests(self):
        await self.set_station('ended')
        self.assertEqual([entry['id'] for entry in await self.store.list()], ['new_one', 'new_two'])

    async def test_no_station_record_lists_everything(self):
        self.assertEqual(len(await self.store.list()), 4)


if __name__ == '__main__':
    unittest.main()
