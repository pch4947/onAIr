"""Board story hand-off: candidate choice, retries, withdrawal and read settlement against real Redis."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.auth import get_current_user
from app.board import BoardStore
from app.board_stories import POSTS_KEY, READ_KEY, STORIES_SINCE_KEY, STORY_PREFIX, StoryFeed, eligible, settle_read
from app.config import Settings
from app.main import create_app
from app.messaging.consumer import EngineConsumer, GROUP
from app.notifications import NotificationStore
from app.streaming.journal import Journal


def post(post_id, body='오늘 시험 끝났어요', age=timedelta(hours=1), author='author-1'):
    created = datetime.now(timezone.utc) - age
    return dict(id=post_id, authorId=author, authorName='글쓴이', title='제목 ' + post_id, body=body,
                songRequestUrl=None, createdAt=created.isoformat(timespec='milliseconds'))


class EligibilityTests(unittest.TestCase):
    def test_long_old_or_pre_notice_posts_are_left_out(self):
        now = datetime.now(timezone.utc).timestamp()
        self.assertTrue(eligible(post('a', 'x' * 500), now, 0))
        self.assertFalse(eligible(post('a', 'x' * 501), now, 0))
        self.assertTrue(eligible(post('a', age=timedelta(days=6)), now, 0))
        self.assertFalse(eligible(post('a', age=timedelta(days=8)), now, 0))
        self.assertFalse(eligible(post('a', age=timedelta(hours=2)), now, now - 3600))


class BoardRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app(Settings('127.0.0.1', 3000))
        self.app.dependency_overrides[get_current_user] = lambda: {'id': 'u1', 'name': '글쓴이'}
        self.app.state.story_feed = SimpleNamespace(offer=AsyncMock(), withdraw=AsyncMock())
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)

    async def test_new_post_is_offered_and_a_failed_offer_does_not_fail_the_post(self):
        self.app.state.board = BoardStore(SimpleNamespace(hset=AsyncMock()))
        body = {'title': '제목', 'body': '내용'}
        reply = await self.client.post('/api/board/posts', json=body)
        self.assertEqual(reply.status_code, 201)
        self.assertEqual(self.app.state.story_feed.offer.await_args.args[0]['id'], reply.json()['post']['id'])
        self.app.state.story_feed.offer.side_effect = RedisConnectionError('offline')
        self.assertEqual((await self.client.post('/api/board/posts', json=body)).status_code, 201)


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires local Redis')
class StoryFeedRedisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Posts and read marks are global keys, so this suite keeps to its own database.
        self.redis = Redis.from_url('redis://127.0.0.1:6379/13', decode_responses=True)
        await self.redis.delete(POSTS_KEY, READ_KEY)
        await self.redis.set(STORIES_SINCE_KEY, '0')
        self.station = 'st_story_' + uuid4().hex
        self.prefix = 'backend:' + self.station
        self.feed = StoryFeed(self.redis, self.station)
        await self.set_station()

    async def asyncTearDown(self):
        keys = [key async for key in self.redis.scan_iter(f'*{self.station}*')]
        keys += [key async for key in self.redis.scan_iter('backend:notifications:*')]
        await self.redis.delete(POSTS_KEY, READ_KEY, STORIES_SINCE_KEY, *keys)
        await self.redis.aclose()

    async def set_station(self, status='started', **config):
        await self.redis.set(self.prefix + ':station', json.dumps(dict(status=status, created_at=1, config=config)))

    async def add(self, *posts):
        for item in posts:
            await self.redis.hset(POSTS_KEY, item['id'], json.dumps(item, ensure_ascii=False))

    async def events(self):
        return [(f['type'], json.loads(f['payload'])) for _, f in await self.redis.xrange(f'engine:{self.station}:in')]

    async def test_start_sends_newest_unread_once_with_nickname_and_prefixed_id(self):
        await self.add(post('new'), post('older', age=timedelta(days=1)), post('read'),
                       post('long', 'x' * 501), post('stale', age=timedelta(days=8)))
        await self.redis.hset(READ_KEY, STORY_PREFIX + 'read', '1')
        await self.feed.offer_recent()
        await self.feed.offer_recent()  # A retried station.started.
        events = await self.events()
        self.assertEqual([p['story_id'] for _, p in events], [STORY_PREFIX + 'new', STORY_PREFIX + 'older'])
        self.assertEqual(events[0][0], 'board.story')
        self.assertEqual({k: events[0][1][k] for k in ('body', 'author_ref', 'nickname', 'tags')},
                         dict(body='오늘 시험 끝났어요', author_ref='author-1', nickname='글쓴이', tags=[]))

    async def test_posts_before_the_first_start_are_never_sent_and_the_start_is_kept(self):
        await self.redis.delete(STORIES_SINCE_KEY)
        await self.add(post('before', age=timedelta(minutes=5)))
        await self.feed.offer_recent()
        self.assertEqual(await self.events(), [])
        first = await self.feed.stories_since()
        self.assertEqual(await self.feed.stories_since(), first)
        await self.feed.offer(post('after', age=timedelta(minutes=-1)))
        self.assertEqual([p['story_id'] for _, p in await self.events()], [STORY_PREFIX + 'after'])

    async def test_candidates_are_capped(self):
        await self.add(*[post(f'p{i}', age=timedelta(minutes=i)) for i in range(33)])
        await self.feed.offer_recent()
        self.assertEqual(len(await self.events()), 30)

    async def test_only_talk_with_a_story_corner_gets_stories(self):
        await self.add(post('one'))
        for config in (dict(format='music'), dict(format='news'),
                       dict(format='talk', corners=['text_corner', 'topic_talk'])):
            await self.set_station(**config)
            await self.feed.offer_recent()
            await self.feed.offer(post('two'))
        self.assertEqual(await self.events(), [])
        await self.set_station(format='talk', corners=['story_corner', 'topic_talk'])
        await self.feed.offer_recent()
        self.assertEqual(len(await self.events()), 1)

    async def test_nothing_is_sent_unless_the_station_is_started(self):
        await self.add(post('one'))
        await self.set_station(status='preparing')
        await self.feed.offer_recent()
        self.assertEqual(await self.events(), [])

    async def test_new_post_offer_and_withdraw_only_what_was_sent(self):
        await self.feed.offer(post('live'))
        await self.feed.withdraw('never-sent')
        await self.feed.withdraw('live')
        await self.feed.withdraw('live')
        self.assertEqual([(t, p['story_id']) for t, p in await self.events()],
                         [('board.story', STORY_PREFIX + 'live'), ('board.story.withdrawn', STORY_PREFIX + 'live')])

    async def test_station_started_event_sends_candidates_after_recording_the_result(self):
        await self.add(post('one'))
        await self.set_station(status='preparing')
        settings = Settings('127.0.0.1', 3000, station_id=self.station)
        consumer = EngineConsumer(self.redis, settings)
        await consumer.ensure_group()
        fields = dict(type='station.started', contract_version='1', event_id='evt_start', station_id=self.station,
                      at='2', payload=json.dumps(dict(topic='시험')))
        entry = await self.redis.xadd(consumer.stream, fields)
        await self.redis.xreadgroup(GROUP, consumer.name, {consumer.stream: '>'})
        await consumer.handle(entry, fields)
        self.assertEqual(json.loads(await self.redis.get(self.prefix + ':station'))['status'], 'started')
        self.assertEqual([p['story_id'] for _, p in await self.events()], [STORY_PREFIX + 'one'])

    async def test_published_story_is_read_once_and_notifies_without_touching_request_state(self):
        await self.add(post('one'))
        await self.redis.set(self.prefix + ':station', json.dumps(dict(
            status='started', created_at=1, config=dict(persona=dict(dj_name='새벽')))))
        segment = dict(id='seg_story', kind='speech', corner_type='story_corner', request_ref=STORY_PREFIX + 'one')
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / 'broadcast.sqlite', self.station)
            try:
                journal.transition(segment, 'MIXING')
                await journal.flush(self.redis)
                self.assertFalse(await self.redis.hexists(READ_KEY, STORY_PREFIX + 'one'))
                for state in ('PUBLISHED', 'PLAYED'):
                    journal.transition(segment, state)
                await journal.flush(self.redis)
            finally:
                journal.close()
        self.assertTrue(await self.redis.hexists(READ_KEY, STORY_PREFIX + 'one'))
        self.assertFalse(await self.redis.exists(self.prefix + ':request-states'))
        await settle_read(self.redis, self.station, STORY_PREFIX + 'one')  # A retried delivery.
        notifications, total = await NotificationStore(self.redis).list('author-1', 0, 10)
        self.assertEqual(total, 1)
        self.assertEqual((notifications[0]['kind'], notifications[0]['data']), ('story_read', dict(postId='one')))
        self.assertIn('새벽 방송', notifications[0]['description'])
        await self.feed.offer(post('one'))  # Read stories are never offered again.
        self.assertEqual([p for t, p in await self.events() if t == 'board.story'], [])
