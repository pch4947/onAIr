"""#279 투표: vote.open을 받아 기준 세그먼트 재생 시작에 열고, 한 청취자 한 표, 창이 닫히면 vote.result."""
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
import uuid

import httpx
from fastapi import Request
from redis.asyncio import Redis

from app.api import PresenceStore
from app.auth import get_current_user
from app.config import Settings
from app.main import create_app
from app.messaging.consumer import EngineConsumer, GROUP
from app.messaging.contracts import VoteOpen, decode
from app.requests import RequestStore
from app.station_runtime import ensure_runtimes
from app.streaming.journal import Journal
from app.test_controls import discard_and_send
from app.votes import close_due

VOTE = dict(vote_id='vote_01', kind='game', question='붕어빵은 머리부터? 꼬리부터?', options=['머리부터', '꼬리부터'],
            segment_id='seg_vote', window_sec=60)


def envelope(data, version='2'):
    return dict(type='vote.open', contract_version=version, event_id='evt_one', station_id='st_test', at='1.0',
                payload=json.dumps(data, ensure_ascii=False))


class VoteOpenContractTests(unittest.TestCase):
    def test_shape(self):
        self.assertEqual(decode(envelope(VOTE), 'st_test'), VoteOpen(**VOTE))
        self.assertEqual(decode(envelope(VOTE, '1'), 'st_test').vote_id, 'vote_01')
        for bad in [VOTE | dict(options=['하나']), VOTE | dict(options=['같음', '같음']),
                    VOTE | dict(options=['a', 'b', 'c']), VOTE | dict(kind='poll'), VOTE | dict(window_sec=5),
                    {k: v for k, v in VOTE.items() if k != 'segment_id'}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                decode(envelope(bad), 'st_test')


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class VoteFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = Redis.from_url('redis://127.0.0.1:6379/0', decode_responses=True)
        self.station = 'st_vote_' + uuid.uuid4().hex
        self.prefix = 'backend:' + self.station
        self.directory = tempfile.TemporaryDirectory()
        settings = Settings('127.0.0.1', 3000, station_id=self.station, audio_dir=Path(self.directory.name))
        self.consumer = EngineConsumer(self.redis, settings)
        await self.consumer.ensure_group()
        await self.redis.set(self.prefix + ':station', json.dumps(dict(status='started', created_at=0, config={})))
        self.app = create_app(settings)
        self.app.state.redis = self.redis
        station = ensure_runtimes(self.app.state)[self.station]
        station.hls = SimpleNamespace(ready=False, finishing=[], active_id=None, current=None, journal=None)
        station.presence, station.requests = PresenceStore(self.redis, self.station), RequestStore(self.redis, self.station)

        def user(request: Request):
            return {'id': request.headers.get('x-user', 'u1'), 'name': 'x', 'kind': 'guest'}
        self.app.dependency_overrides[get_current_user] = user
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        keys = [k async for k in self.redis.scan_iter(f'*{self.station}*')]
        if keys:
            await self.redis.delete(*keys)
        await self.redis.aclose()
        self.directory.cleanup()

    async def deliver_vote(self, data=VOTE):
        fields = dict(type='vote.open', contract_version='2', event_id='evt_' + uuid.uuid4().hex,
                      station_id=self.station, at='1', payload=json.dumps(data, ensure_ascii=False))
        entry_id = await self.redis.xadd(self.consumer.stream, fields)
        await self.redis.xreadgroup(GROUP, self.consumer.name, {self.consumer.stream: '>'})
        await self.consumer.handle(entry_id, fields)

    async def start_segment(self, segment_id, started_at, failed=False):
        journal = Journal(Path(self.directory.name) / f'{uuid.uuid4().hex}.sqlite', self.station)
        try:
            segment = dict(id=segment_id, kind='speech', corner_type='dj_vs_listeners', request_ref=None)
            if failed:
                journal.transition(segment, 'FAILED')
            else:
                journal.segment_started(segment, started_at)
            await journal.flush(self.redis)
        finally:
            journal.close()

    async def ballot(self, user, option, vote_id='vote_01'):
        return await self.client.post(f'/api/stations/{self.station}/votes/{vote_id}', json={'option': option},
                                      headers={'x-user': user})

    async def results(self):
        return [json.loads(f['payload']) for _, f in await self.redis.xrange(f'engine:{self.station}:in')
                if f['type'] == 'vote.result']

    async def test_opens_on_its_segment_counts_one_ballot_each_and_reports_once(self):
        await self.deliver_vote()
        self.assertEqual((await self.ballot('a', '머리부터')).status_code, 404)  # 기준 멘트가 나오기 전
        started = time.time() - 1
        await self.start_segment('seg_vote', started)
        vote = json.loads(await self.redis.hget(self.prefix + ':votes', 'vote_01'))
        self.assertEqual(vote['status'], 'open')
        # Redis Lua의 cjson은 숫자를 유효숫자 14자리로 저장한다 — 창 길이에 영향이 없는 수십 마이크로초 차이
        self.assertAlmostEqual(vote['opens_at'], started, delta=0.001)
        self.assertAlmostEqual(vote['closes_at'], started + 60, delta=0.001)
        self.assertFalse(await self.redis.hexists(self.prefix + ':vote-pending', 'seg_vote'))

        self.assertEqual((await self.ballot('a', '머리부터')).json(), {'vote_id': 'vote_01', 'mine': '머리부터'})
        self.assertEqual((await self.ballot('a', '머리부터')).status_code, 200)  # 같은 표를 다시 보내도 한 표
        changed = await self.ballot('a', '꼬리부터')
        self.assertEqual((changed.status_code, changed.json()['detail']['mine']), (409, '머리부터'))
        self.assertEqual((await self.ballot('b', '붕어빵')).status_code, 422)
        self.assertEqual((await self.ballot('b', '꼬리부터')).status_code, 200)
        self.assertEqual((await self.ballot('c', '머리부터')).status_code, 200)

        current = (await self.client.get(f'/api/stations/{self.station}/votes/current', headers={'x-user': 'b'})).json()
        self.assertEqual((current['vote']['status'], current['vote']['mine']), ('open', '꼬리부터'))
        self.assertNotIn('counts', current['vote'])  # 열린 동안에는 표 수를 내리지 않는다
        none_yet = (await self.client.get(f'/api/stations/{self.station}/votes/current', headers={'x-user': 'z'})).json()
        self.assertIsNone(none_yet['vote']['mine'])
        state = (await self.client.get(f'/api/stations/{self.station}/stream/state')).json()
        self.assertEqual(state['currentVote']['question'], VOTE['question'])

        self.assertEqual(await close_due(self.redis, self.station, started + 59), 0)
        self.assertEqual(await close_due(self.redis, self.station, started + 60.01), 1)  # 저장 정밀도(약 0.1ms) 여유
        self.assertEqual(await close_due(self.redis, self.station, started + 61), 0)
        self.assertEqual(await self.results(), [dict(vote_id='vote_01', counts={'머리부터': 2, '꼬리부터': 1}, total=3)])

    async def test_closed_vote_shows_its_result_and_takes_no_ballots(self):
        await self.deliver_vote()
        await self.start_segment('seg_vote', time.time() - 61)  # 이미 창이 지났다
        self.assertEqual((await self.ballot('a', '머리부터')).status_code, 409)
        current = (await self.client.get(f'/api/stations/{self.station}/votes/current')).json()['vote']
        self.assertEqual((current['status'], current['counts'], current['total']), ('closed', {'머리부터': 0, '꼬리부터': 0}, 0))
        self.assertEqual(await self.results(), [dict(vote_id='vote_01', counts={'머리부터': 0, '꼬리부터': 0}, total=0)])

    async def test_a_discarded_or_failed_base_segment_never_opens_its_vote(self):
        await self.deliver_vote()
        await self.redis.hset(self.prefix + ':segments', 'seg_vote', json.dumps(dict(id='seg_vote', kind='speech')))
        await self.redis.rpush(self.prefix + ':queue', 'seg_vote')
        state = ensure_runtimes(self.app.state)[self.station]
        self.assertEqual(await discard_and_send(state, self.station, 'corner.skip', {'corner': 'topic_talk'}), 1)
        await self.start_segment('seg_vote', time.time() - 1)
        self.assertFalse(await self.redis.hexists(self.prefix + ':votes', 'vote_01'))
        await self.deliver_vote(VOTE | dict(vote_id='vote_02', segment_id='seg_fail'))
        await self.start_segment('seg_fail', 0, failed=True)
        self.assertFalse(await self.redis.hexists(self.prefix + ':vote-pending', 'seg_fail'))
        self.assertIsNone((await self.client.get(f'/api/stations/{self.station}/votes/current')).json()['vote'])


if __name__ == '__main__':
    unittest.main()
