"""Contract v2 (#157 step 1·3): the backend accepts engine events in v1 and v2, and sends request.arrived as v2."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from redis.asyncio import Redis

from app.board_stories import POSTS_KEY, READ_KEY, STORY_PREFIX
from app.config import Settings
from app.messaging.consumer import EngineConsumer, GROUP
from app.messaging.contracts import CornerChanged, RequestState, Segment, decode
from app.requests import RequestStore
from app.streaming.journal import Journal
from test_consumer import payload


def envelope(kind, data, version='2', station='st_test'):
    return dict(type=kind, contract_version=version, event_id='evt_one', station_id=station, at='1.0',
                payload=json.dumps(data))


def v2_segment(**extra):
    data = {k: v for k, v in payload().items() if k != 'request_ref'}
    return data | dict(input_refs=['req_a', 'req_b'], reflection_levels={'req_a': 2, 'req_b': 3}) | extra


class DecodeTests(unittest.TestCase):
    def test_v2_segment_is_stored_with_its_inputs(self):
        segment = decode(envelope('segment.submitted', v2_segment()), 'st_test')
        self.assertIsInstance(segment, Segment)
        self.assertIsNone(segment.request_ref)
        self.assertEqual((segment.input_refs, segment.reflection_levels), (['req_a', 'req_b'], {'req_a': 2, 'req_b': 3}))
        empty = decode(envelope('segment.submitted', v2_segment(input_refs=[], reflection_levels={})), 'st_test')
        self.assertEqual((empty.input_refs, empty.reflection_levels), ([], {}))

    def test_v1_segment_is_unchanged(self):
        segment = decode(envelope('segment.submitted', payload(), version='1'), 'st_test')
        self.assertEqual(segment.model_dump(exclude_none=True), {k: v for k, v in payload().items() if v is not None})
        self.assertIsNone(segment.input_refs)

    def test_mixed_or_bad_segments_are_rejected(self):
        bad = [('2', v2_segment(request_ref=None)),  # v1 field in v2
               ('2', v2_segment(reflection_levels={'req_a': 2})),  # keys must match input_refs
               ('2', v2_segment(reflection_levels={'req_a': 2, 'req_b': 5})),  # levels are 1~4
               ('2', {k: v for k, v in v2_segment().items() if k != 'input_refs'}),
               ('1', payload() | dict(input_refs=['req_a'])),  # v2 field in v1
               ('3', payload())]
        for version, data in bad:
            with self.subTest(version=version, data=data), self.assertRaises(ValueError):
                decode(envelope('segment.submitted', data, version), 'st_test')

    def test_request_state_values_follow_the_contract_version(self):
        for state in ('pooled', 'inboxed', 'selected', 'not_on_air', 'cancelled', 'expired', 'rejected'):
            self.assertEqual(decode(envelope('request.state', dict(request_id='req_a', state=state)), 'st_test').state,
                             state)
        reflected = decode(envelope('request.state', dict(request_id='req_a', state='reflected', segment_id='seg_one',
                                                          started_at=12.5)), 'st_test')
        self.assertEqual(reflected, RequestState(request_id='req_a', state='reflected', segment_id='seg_one',
                                                 started_at=12.5))
        for version, data in [('2', dict(request_id='req_a', state='screened')),
                              ('1', dict(request_id='req_a', state='pooled')),
                              ('1', dict(request_id='req_a', state='expired', segment_id='seg_one')),
                              ('2', dict(request_id='req_a', state='unknown'))]:
            with self.subTest(version=version, data=data), self.assertRaises(ValueError):
                decode(envelope('request.state', data, version), 'st_test')

    def test_corner_changed_may_carry_the_format(self):
        good = dict(corner='headlines', title='헤드라인', contract=dict(chat='closed'), segment_id='seg_one')
        self.assertIsNone(decode(envelope('corner.changed', good), 'st_test').format)
        self.assertEqual(decode(envelope('corner.changed', good | dict(format='news')), 'st_test'),
                         CornerChanged(**good, format='news'))
        with self.assertRaises(ValueError):
            decode(envelope('corner.changed', good | dict(format='sports')), 'st_test')


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class ConsumerV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = Redis.from_url('redis://127.0.0.1:6379/0', decode_responses=True)
        self.directory = tempfile.TemporaryDirectory()
        self.station = 'st_v2_' + uuid.uuid4().hex
        self.settings = Settings('127.0.0.1', 3000, station_id=self.station, audio_dir=Path(self.directory.name))
        self.consumer = EngineConsumer(self.redis, self.settings)
        await self.consumer.ensure_group()

    async def asyncTearDown(self):
        for pattern in [f'backend:{self.station}:*', f'engine:{self.station}:*']:
            keys = [key async for key in self.redis.scan_iter(match=pattern)]
            if keys:
                await self.redis.delete(*keys)
        await self.redis.aclose()
        self.directory.cleanup()

    async def deliver(self, data, kind, version='2'):
        fields = dict(type=kind, contract_version=version, event_id='evt_' + uuid.uuid4().hex,
                      station_id=self.station, at='1', payload=json.dumps(data))
        entry_id = await self.redis.xadd(self.consumer.stream, fields)
        await self.redis.xreadgroup(GROUP, self.consumer.name, {self.consumer.stream: '>'})
        await self.consumer.handle(entry_id, fields)

    async def state(self, request_id):
        raw = await self.redis.hget(self.consumer.prefix + ':request-states', request_id)
        return json.loads(raw) if raw else None

    async def test_v2_states_move_and_end_at_a_terminal_state(self):
        for state in ('pooled', 'selected', 'pooled', 'selected'):
            await self.deliver(dict(request_id='req_a', state=state), 'request.state')
        await self.deliver(dict(request_id='req_a', state='reflected', segment_id='seg_one', started_at=5.0),
                           'request.state')
        self.assertEqual(await self.state('req_a'),
                         dict(request_id='req_a', state='reflected', segment_id='seg_one', started_at=5.0))
        await self.deliver(dict(request_id='req_a', state='pooled'), 'request.state')  # 종결 뒤에는 안 바뀐다
        self.assertEqual((await self.state('req_a'))['state'], 'reflected')
        history = [json.loads(row) for row in await self.redis.lrange(
            self.consumer.prefix + ':request-history:req_a', 0, -1)]
        self.assertEqual([(h['state'], h['accepted']) for h in history][-2:], [('reflected', True), ('pooled', False)])
        for state in ('inboxed', 'not_on_air'):
            await self.deliver(dict(request_id='req_b', state=state), 'request.state')
        self.assertEqual((await self.state('req_b'))['state'], 'not_on_air')
        self.assertEqual(await self.redis.xlen(self.consumer.stream + ':dead'), 0)

    async def test_a_request_can_move_from_v1_to_v2_when_the_engine_switches(self):
        await self.deliver(dict(request_id='req_c', state='queued'), 'request.state', version='1')
        await self.deliver(dict(request_id='req_c', state='selected'), 'request.state')
        self.assertEqual((await self.state('req_c'))['state'], 'selected')
        self.assertEqual(json.loads(await self.redis.hget(self.consumer.prefix + ':request-states', 'req_c')),
                         dict(request_id='req_c', state='selected'))  # v1·v2 공통 형식, 빈 필드는 싣지 않는다

    async def test_v2_segment_is_queued_and_its_corner_format_is_kept(self):
        audio = Path(self.directory.name) / self.station / 'one.wav'
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b'RIFF')
        segment = v2_segment(station_id=self.station, audio_ref=f'{self.station}/one.wav')
        await self.deliver(segment, 'segment.submitted')
        stored = json.loads(await self.redis.hget(self.consumer.prefix + ':segments', 'seg_one'))
        self.assertEqual((stored['request_ref'], stored['input_refs']), (None, ['req_a', 'req_b']))
        self.assertEqual(await self.redis.lrange(self.consumer.prefix + ':queue', 0, -1), ['seg_one'])
        await self.deliver(dict(corner='headlines', title='헤드라인', contract=dict(chat='closed'),
                                segment_id='seg_one', format='news'), 'corner.changed')
        self.assertEqual(json.loads(await self.redis.hget(self.consumer.prefix + ':corner-pending', 'seg_one'))['format'],
                         'news')
        self.assertEqual(await self.redis.xlen(self.consumer.stream + ':dead'), 0)


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class PlayoutV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Posts and read marks are global keys, so this suite keeps to its own database.
        self.redis = Redis.from_url('redis://127.0.0.1:6379/13', decode_responses=True)
        self.station = 'st_v2play_' + uuid.uuid4().hex
        self.prefix = 'backend:' + self.station
        await self.redis.delete(POSTS_KEY, READ_KEY)
        await self.redis.set(self.prefix + ':station', json.dumps(dict(status='started', created_at=1, config={})))

    async def asyncTearDown(self):
        keys = [key async for key in self.redis.scan_iter(f'*{self.station}*')]
        keys += [key async for key in self.redis.scan_iter('backend:notifications:*')]
        await self.redis.delete(POSTS_KEY, READ_KEY, *keys)
        await self.redis.aclose()

    async def test_v2_playout_leaves_request_state_to_the_engine_and_reads_every_story(self):
        for post_id in ('one', 'two'):
            await self.redis.hset(POSTS_KEY, post_id, json.dumps(dict(
                id=post_id, authorId='author-' + post_id, authorName='글쓴이', title='제목 ' + post_id, body='본문',
                songRequestUrl=None, createdAt='2026-11-14T00:00:00.000+00:00')))
        await self.redis.hset(self.prefix + ':request-states', 'req_chat', json.dumps(dict(request_id='req_chat',
                                                                                             state='selected')))
        segment = dict(id='seg_v2', kind='speech', corner_type='story_corner', request_ref=None,
                       input_refs=[STORY_PREFIX + 'one', 'req_chat', STORY_PREFIX + 'two'],
                       reflection_levels={STORY_PREFIX + 'one': 3, 'req_chat': 2, STORY_PREFIX + 'two': 2})
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / 'broadcast.sqlite', self.station)
            try:
                for state in ('MIXING', 'PUBLISHED', 'PLAYED'):
                    journal.transition(segment, state)
                await journal.flush(self.redis)
            finally:
                journal.close()
        self.assertEqual(json.loads(await self.redis.hget(self.prefix + ':request-states', 'req_chat'))['state'],
                         'selected')  # 송출 상태(published·played)로 바꾸지 않는다
        self.assertEqual(sorted(await self.redis.hkeys(READ_KEY)), [STORY_PREFIX + 'one', STORY_PREFIX + 'two'])


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class ArrivedV2Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = Redis.from_url('redis://127.0.0.1:6379/0', decode_responses=True)
        self.station = 'st_v2in_' + uuid.uuid4().hex
        self.store = RequestStore(self.redis, self.station)

    async def asyncTearDown(self):
        keys = [key async for key in self.redis.scan_iter(f'*{self.station}*')]
        if keys:
            await self.redis.delete(*keys)
        await self.redis.aclose()

    async def test_arrivals_go_out_as_v2_and_story_is_sent_as_chat(self):
        chat = await self.store.create('채팅이에요', 'u1', 'story')
        message = await self.store.create('사연이에요', 'u1', 'message', listener_name='청취자')
        self.assertEqual((chat['kind'], message['kind']), ('story', 'message'))  # 저장된 기록은 그대로
        events = [(f['contract_version'], json.loads(f['payload'])) for _, f in
                  await self.redis.xrange(f'engine:{self.station}:in')]
        self.assertEqual([(v, p['kind']) for v, p in events], [('2', 'chat'), ('2', 'message')])
        self.assertEqual(events[1][1]['nickname'], '청취자')

    async def test_v2_terminal_states_leave_the_pending_count(self):
        ids = [(await self.store.create(f'요청 {i}', 'u1', 'chat'))['id'] for i in range(4)]
        for request_id, state in zip(ids, ('reflected', 'cancelled', 'not_on_air', 'selected')):
            await self.redis.hset(self.store.prefix + ':request-states', request_id,
                                  json.dumps(dict(request_id=request_id, state=state)))
        self.assertEqual((await self.store.metrics())['pendingRequestCount'], 1)


if __name__ == '__main__':
    unittest.main()
