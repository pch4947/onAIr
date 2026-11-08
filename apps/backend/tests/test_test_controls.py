"""테스트 화면 코너 건너뛰기: 어드민만, 재생 안 한 멘트만 버리고 엔진에 corner.skip·segment.discarded 발행."""
from collections import deque
import json
import os
from types import SimpleNamespace
import unittest
import uuid

import httpx
from redis.asyncio import Redis

from app.auth import get_current_user
from app.config import Settings
from app.main import create_app
from app.test_controls import discardable_segments, public_decision
from app.station_runtime import default_runtime


def segment(key, kind='speech'):
    return json.dumps({'id': key, 'kind': kind, 'corner_type': 'topic_talk', 'duration_ms': 3000})


class FakeJournal:
    def __init__(self, claimed):
        self.claimed = claimed

    def status(self, key):
        return self.claimed.get(key)


def publisher(current=None, active_id=None, finishing=(), claimed=None):
    return SimpleNamespace(current={'id': current} if current else None, active_id=active_id,
                           finishing=deque((0, {'id': key}) for key in finishing), journal=FakeJournal(claimed or {}))


class DiscardableSegmentTests(unittest.TestCase):
    def test_only_untouched_speech_is_discarded(self):
        ids = ['playing', 'encoding', 'finishing', 'mixed', 'song', 'speech1', 'glue1', 'missing']
        records = {key: segment(key) for key in ids if key not in ('song', 'glue1', 'missing')}
        records |= {'song': segment('song', 'music'), 'glue1': segment('glue1', 'glue')}
        chosen = discardable_segments(publisher(current='playing', active_id='encoding', finishing=['finishing'],
                                                claimed={'mixed': 'MIXING'}), ids, records)
        self.assertEqual(chosen, ['speech1', 'glue1'])

    def test_nothing_to_discard_on_an_empty_queue(self):
        self.assertEqual(discardable_segments(publisher(), [], {}), [])


class PublicDecisionTests(unittest.TestCase):
    def test_quotes_carry_classification_without_requester(self):
        chat = {'request_id': 'r1', 'kind': 'chat', 'body': '컵라면이요', 'requester_ref': 'u1',
                'chat_type': 'standalone', 'rule': 'default', 'state': 'pooled', 'nickname': '밤샘러'}
        entry = {'id': 7, 'at': 10.0,
                 'context': {'corner': 'topic_talk', 'pending_requests': [chat]},
                 'decision': {'action': 'generate', 'corner_type': 'topic_talk', 'step': 'talk',
                              'quotes': ['r1', 'gone'], 'situation': 'normal', 'criterion': 'relevance'}}
        shown = public_decision(entry)
        self.assertEqual((shown['quotes'][0]['chat_type'], shown['quotes'][0]['rule']), ('standalone', 'default'))
        self.assertEqual(shown['quotes'][1], {'request_id': 'gone', 'kind': None, 'body': None,
                                              'chat_type': None, 'rule': None, 'state': None, 'nickname': None})
        self.assertNotIn('requester_ref', json.dumps(shown))
        self.assertEqual((shown['corner'], shown['step'], shown['criterion']), ('topic_talk', 'talk', 'relevance'))


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class CornerSkipApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.station = 'st_skip_' + uuid.uuid4().hex
        self.prefix = f'backend:{self.station}'
        self.redis = Redis.from_url('redis://127.0.0.1:6379/15', decode_responses=True)
        self.app = create_app(Settings('127.0.0.1', 3000, station_id=self.station, hls_enabled=True))
        self.app.state.redis = self.redis
        default_runtime(self.app).hls = publisher(current='playing')
        self.user = {'id': 'a1', 'kind': 'admin'}
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        await self.redis.set(self.prefix + ':station', json.dumps({'status': 'started'}))
        ids = ['playing', 'song', 'speech1', 'speech2']
        await self.redis.rpush(self.prefix + ':queue', *ids)
        await self.redis.hset(self.prefix + ':segments', mapping={
            key: segment(key, 'music' if key == 'song' else 'speech') for key in ids})
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://test')
        self.path = f'/api/stations/{self.station}/corner-skip'

    async def asyncTearDown(self):
        await self.client.aclose()
        keys = [k async for k in self.redis.scan_iter(match=f'{self.prefix}:*')]
        keys += [k async for k in self.redis.scan_iter(match=f'engine:{self.station}:*')]
        if keys:
            await self.redis.delete(*keys)
        await self.redis.aclose()

    async def events(self):
        return [(f['type'], json.loads(f['payload'])) for _, f in await self.redis.xrange(f'engine:{self.station}:in')]

    async def test_skip_discards_unplayed_speech_and_tells_the_engine(self):
        response = await self.client.post(self.path, json={'corner': 'text_corner'})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {'corner': 'text_corner', 'discarded': 2})
        self.assertEqual(await self.redis.lrange(self.prefix + ':queue', 0, -1), ['playing', 'song'])
        self.assertEqual(await self.events(), [('segment.discarded', {'segment_ids': ['speech1', 'speech2']}),
                                               ('corner.skip', {'corner': 'text_corner'})])
        self.assertEqual(await self.redis.hlen(self.prefix + ':segments'), 4)  # 기록은 지우지 않는다

        again = await self.client.post(self.path, json={'corner': 'topic_talk'})
        self.assertEqual(again.json()['discarded'], 0)
        self.assertEqual((await self.events())[-1], ('corner.skip', {'corner': 'topic_talk'}))

    async def test_only_admins_on_a_started_station_with_a_valid_corner(self):
        self.user = {'id': 'u1', 'kind': 'member'}
        self.assertEqual((await self.client.post(self.path, json={'corner': 'text_corner'})).status_code, 403)
        self.user = {'id': 'a1', 'kind': 'admin'}
        for corner in ['', 'Text', 'text-corner', 'x' * 41]:
            with self.subTest(corner=corner):
                self.assertEqual((await self.client.post(self.path, json={'corner': corner})).status_code, 422)
        self.assertEqual((await self.client.post(
            '/api/stations/other/corner-skip', json={'corner': 'text_corner'})).status_code, 404)
        await self.redis.set(self.prefix + ':station', json.dumps({'status': 'preparing'}))
        self.assertEqual((await self.client.post(self.path, json={'corner': 'text_corner'})).status_code, 409)
        self.assertEqual(await self.events(), [])
        self.assertEqual(await self.redis.llen(self.prefix + ':queue'), 4)



def decision(key, at, action='generate', quotes=()):
    chat = {'request_id': 'r1', 'kind': 'chat', 'body': '안녕', 'requester_ref': 'u1', 'chat_type': 'reaction',
            'rule': 'short_reaction', 'state': 'pooled', 'nickname': None}
    return {'id': key, 'at': at, 'context': {'corner': 'topic_talk', 'pending_requests': [chat]},
            'decision': {'action': action, 'corner_type': 'topic_talk', 'step': 'talk', 'quotes': list(quotes),
                         'situation': 'normal', 'criterion': 'recency'}}


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class DecisionApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.station = 'st_dec_' + uuid.uuid4().hex
        self.redis = Redis.from_url('redis://127.0.0.1:6379/15', decode_responses=True)
        self.app = create_app(Settings('127.0.0.1', 3000, station_id=self.station, hls_enabled=True))
        self.app.state.redis = self.redis
        self.user = {'id': 'a1', 'kind': 'admin'}
        self.app.dependency_overrides[get_current_user] = lambda: self.user
        await self.redis.set(f'backend:{self.station}:station', json.dumps({'status': 'started', 'created_at': 100.0}))
        # 지난 방송(at < 100)과 이번 방송 결정이 섞인 엔진 로그. 한 페이지 2건씩
        self.log = [decision(1, 50.0), decision(2, 60.0, 'wait'), decision(3, 101.0, quotes=['r1']),
                    decision(4, 102.0, 'wait'), decision(5, 103.0, 'glue')]
        self.calls = []

        def engine(request):
            self.calls.append(dict(request.url.params))
            if self.down:
                return httpx.Response(503)
            after = int(request.url.params.get('after', 0))
            page = [d for d in self.log if d['id'] > after][:2]
            more = any(d['id'] > (page[-1]['id'] if page else after) for d in self.log)
            return httpx.Response(200, json={'decisions': page, 'next': page[-1]['id'] if more else None})
        self.down = False
        self.app.state.engine_http = httpx.AsyncClient(transport=httpx.MockTransport(engine), base_url='http://engine')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://test')
        self.path = f'/api/stations/{self.station}/decisions'

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.app.state.engine_http.aclose()
        await self.redis.delete(f'backend:{self.station}:station')
        await self.redis.aclose()

    async def test_only_this_broadcast_without_waits_or_requester(self):
        import app.test_controls as controls
        page, controls.DECISION_PAGE = controls.DECISION_PAGE, 2
        try:
            body = (await self.client.get(self.path)).json()
            self.assertEqual([d['id'] for d in body['decisions']], [3, 5])
            self.assertEqual(body['next'], 5)
            self.assertEqual(body['decisions'][0]['quotes'][0]['rule'], 'short_reaction')
            self.assertNotIn('requester_ref', json.dumps(body))

            self.log.append(decision(6, 104.0))
            self.assertEqual([d['id'] for d in (await self.client.get(self.path, params={'after': 5})).json()['decisions']], [6])

            # 새로 들어와도 지난 방송 구간(1~2)은 다시 읽지 않는다
            self.calls.clear()
            await self.client.get(self.path)
            self.assertEqual(self.calls[0].get('after'), '2')
        finally:
            controls.DECISION_PAGE = page

    async def test_errors(self):
        self.user = {'id': 'u1', 'kind': 'member'}
        self.assertEqual((await self.client.get(self.path)).status_code, 403)
        self.user = {'id': 'a1', 'kind': 'admin'}
        self.assertEqual((await self.client.get('/api/stations/other/decisions')).status_code, 404)
        self.assertEqual((await self.client.get(self.path, params={'after': -1})).status_code, 422)
        self.down = True
        self.assertEqual((await self.client.get(self.path)).status_code, 503)
        await self.redis.delete(f'backend:{self.station}:station')
        self.down = False
        self.assertEqual((await self.client.get(self.path)).json(), {'decisions': [], 'next': None})


if __name__ == '__main__':
    unittest.main()
