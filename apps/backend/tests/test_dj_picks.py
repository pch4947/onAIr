"""DJ 선곡 — 엔진이 낸 곡 후보를 찾아 대기열에 넣는다 (계약 3.4절 track.suggest, #295)."""
import asyncio
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from redis.asyncio import Redis

from app import dj_picks
from app.dj_picks import resolve_pick
from app.config import Settings
from app.messaging.consumer import GROUP, EngineConsumer
from app.messaging.contracts import TrackSuggest, decode
from app.music import ENQUEUE, TrackCandidate
from app.streaming.tracks import deliver_start
from test_broadcast_runtime import segment


def suggest(*titles, suggest_id='sg_1'):
    return TrackSuggest(suggest_id=suggest_id,
                        candidates=[{'title': title, 'artist': '가수'} for title in titles or ('밤편지',)])


def hit(video_id='aaaaaaaaaaa', title='찾은 곡'):
    return TrackCandidate(video_id=video_id, url='https://www.youtube.com/watch?v=' + video_id, title=title,
                          channel='채널', duration_ms=200_000, thumbnail_url='https://i.ytimg.com/x.jpg')


def fake_state(*, has_next=False, known=(), first=True):
    redis = AsyncMock()
    redis.set.return_value = first
    redis.hexists.return_value = has_next
    redis.hvals.return_value = [json.dumps({'track': {'track_ref': ref}}) for ref in known]
    music = SimpleNamespace(lock=asyncio.Lock(), enqueue=AsyncMock(return_value={'status': 'queued'}))
    settings = SimpleNamespace(station_id='st_test')
    return SimpleNamespace(redis=redis, settings=settings, music=music)


def failed_reason(state):
    """엔진에 보낸 track.suggest_failed의 사유. 보내지 않았으면 None."""
    if not state.redis.xadd.await_count:
        return None
    stream, fields = state.redis.xadd.await_args.args
    assert stream == 'engine:st_test:in' and fields['type'] == 'track.suggest_failed'
    return json.loads(fields['payload'])


class ContractTests(unittest.TestCase):
    def fields(self, payload):
        return dict(type='track.suggest', contract_version='2', event_id='evt_1', station_id='st_test', at='1',
                    payload=json.dumps(payload))

    def test_track_suggest_is_decoded_in_both_contract_versions(self):
        payload = {'suggest_id': 'sg_1', 'candidates': [{'title': '밤편지', 'artist': '아이유'}, {'title': '숲'}]}
        for version in ('1', '2'):
            event = decode({**self.fields(payload), 'contract_version': version}, 'st_test')
            self.assertIsInstance(event, TrackSuggest)
            self.assertEqual([(c.title, c.artist) for c in event.candidates], [('밤편지', '아이유'), ('숲', '')])

    def test_malformed_suggestions_are_rejected(self):
        for bad in ({'suggest_id': 'sg_1', 'candidates': []},
                    {'suggest_id': 'sg_1'},
                    {'suggest_id': '', 'candidates': [{'title': '곡'}]},
                    {'suggest_id': 'sg_1', 'candidates': [{'title': ''}]},
                    {'suggest_id': 'sg_1', 'candidates': [{'title': '곡', 'url': 'https://example.com'}]},
                    {'suggest_id': 'sg_1', 'candidates': [{'title': '곡'}] * 6}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                decode(self.fields(bad), 'st_test')


class ResolvePickTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_candidate_found_is_queued_as_a_dj_song(self):
        state = fake_state()
        with patch.object(dj_picks, 'search_youtube', AsyncMock(return_value=[hit(), hit('bbbbbbbbbbb')])) as search:
            self.assertEqual(await resolve_pick(state, suggest('밤편지', '숲')), 'queued')
        search.assert_awaited_once_with(state.settings, '가수 밤편지')
        state.music.enqueue.assert_awaited_once_with(
            'aaaaaaaaaaa', 'dj', 'sg_1:0', 'dj', extra={'suggest_id': 'sg_1', 'candidate': 0})
        self.assertIsNone(failed_reason(state))

    async def test_missing_or_already_played_candidates_fall_through_to_the_next_one(self):
        state = fake_state(known=['yt:aaaaaaaaaaa'])
        results = [[], [hit('aaaaaaaaaaa')], [hit('ccccccccccc')]]  # 못 찾음, 이미 튼 곡, 새 곡
        with patch.object(dj_picks, 'search_youtube', AsyncMock(side_effect=results)):
            self.assertEqual(await resolve_pick(state, suggest('없는 곡', '튼 곡', '새 곡')), 'queued')
        state.music.enqueue.assert_awaited_once_with(
            'ccccccccccc', 'dj', 'sg_1:2', 'dj', extra={'suggest_id': 'sg_1', 'candidate': 2})

    async def test_live_and_cover_videos_are_skipped_for_the_original(self):
        state = fake_state()
        results = [hit('aaaaaaaaaaa', '아이유 - 밤편지 [LIVE] 콘서트'), hit('bbbbbbbbbbb', '밤편지 (Cover by 누구)'),
                   hit('ccccccccccc', '[MV] IU(아이유) _ Through the Night(밤편지)')]
        with patch.object(dj_picks, 'search_youtube', AsyncMock(return_value=results)):
            self.assertEqual(await resolve_pick(state, suggest('밤편지')), 'queued')
        self.assertEqual(state.music.enqueue.await_args.args[0], 'ccccccccccc')

    async def test_only_live_results_fall_through_then_report_not_found(self):
        state = fake_state()
        results = [[hit('aaaaaaaaaaa', '밤편지 라이브'), hit('bbbbbbbbbbb', '밤편지 1시간')], [hit('ccccccccccc', '숲')]]
        with patch.object(dj_picks, 'search_youtube', AsyncMock(side_effect=results)):
            self.assertEqual(await resolve_pick(state, suggest('밤편지', '숲')), 'queued')
        self.assertEqual(state.music.enqueue.await_args.args[:3], ('ccccccccccc', 'dj', 'sg_1:1'))
        state = fake_state()
        with patch.object(dj_picks, 'search_youtube', AsyncMock(return_value=[hit(title='밤편지 직캠')])):
            self.assertEqual(await resolve_pick(state, suggest('밤편지')), 'not_found')
        self.assertEqual(failed_reason(state), {'suggest_id': 'sg_1', 'reason': 'not_found'})

    def test_marks_are_whole_words_and_a_candidate_with_the_mark_is_exempt(self):
        def chosen(wanted, *titles):
            candidate = suggest(wanted).candidates[0]
            found = dj_picks.original_hit([hit(title=title) for title in titles], candidate)
            return found and found.title

        for title in ('Alive', 'Deliver Me', 'Discover', 'Mr. Chu', 'Instant Crush', 'Olivetti'):
            with self.subTest(kept=title):
                self.assertEqual(chosen('곡', title), title)
        for title in ('Song (Live)', 'song live at wembley', 'Song COVER', 'Song MR', 'Song (Inst.)',
                      'Song Instrumental', 'Song 1 Hour Loop', 'Song fancam', '노래방 Song', '히트곡 모음'):
            with self.subTest(skipped=title):
                self.assertIsNone(chosen('곡', title))
        self.assertEqual(chosen('Live Forever', 'Oasis - Live Forever'), 'Oasis - Live Forever')
        # 면제는 그 말에만 든다 — 제목에 Live가 있어도 커버는 거른다
        self.assertEqual(chosen('Live Forever', 'Live Forever (cover)', 'Live Forever'), 'Live Forever')

    async def test_nothing_found_is_reported_to_the_engine(self):
        state = fake_state()
        with patch.object(dj_picks, 'search_youtube', AsyncMock(return_value=[])):
            self.assertEqual(await resolve_pick(state, suggest('없는 곡', '또 없는 곡')), 'not_found')
        state.music.enqueue.assert_not_awaited()
        self.assertEqual(failed_reason(state), {'suggest_id': 'sg_1', 'reason': 'not_found'})

    async def test_suggestion_is_not_needed_when_a_next_song_is_already_planned(self):
        state = fake_state(has_next=True)
        with patch.object(dj_picks, 'search_youtube', AsyncMock()) as search:
            self.assertEqual(await resolve_pick(state, suggest()), 'not_needed')
        search.assert_not_awaited()
        self.assertEqual(failed_reason(state)['reason'], 'not_needed')

    async def test_redelivered_suggestion_is_ignored(self):
        state = fake_state(first=None)
        with patch.object(dj_picks, 'search_youtube', AsyncMock()) as search:
            self.assertEqual(await resolve_pick(state, suggest()), 'duplicate')
        search.assert_not_awaited()
        self.assertIsNone(failed_reason(state))

    async def test_download_failure_tries_the_next_candidate_then_reports_unavailable(self):
        state = fake_state()
        state.music.enqueue.side_effect = HTTPException(502, 'Audio preparation failed')
        with patch.object(dj_picks, 'search_youtube',
                          AsyncMock(side_effect=[[hit()], [hit('bbbbbbbbbbb')]])):
            self.assertEqual(await resolve_pick(state, suggest('첫째', '둘째')), 'unavailable')
        self.assertEqual(state.music.enqueue.await_count, 2)
        self.assertEqual(failed_reason(state)['reason'], 'unavailable')

    async def test_search_outage_is_unavailable_and_busy_download_is_busy(self):
        state = fake_state()
        with patch.object(dj_picks, 'search_youtube', AsyncMock(side_effect=HTTPException(503, 'down'))):
            self.assertEqual(await resolve_pick(state, suggest()), 'unavailable')
        state = fake_state()
        state.music.enqueue.side_effect = HTTPException(429, 'busy')
        with patch.object(dj_picks, 'search_youtube', AsyncMock(return_value=[hit()])):
            self.assertEqual(await resolve_pick(state, suggest()), 'busy')

    async def test_waits_for_a_running_download_and_gives_up_when_it_takes_too_long(self):
        state = fake_state()
        await state.music.lock.acquire()
        with patch.object(dj_picks, 'search_youtube', AsyncMock(return_value=[hit()])), \
                patch.object(dj_picks, 'LOCK_WAIT_SEC', 0.0):
            self.assertEqual(await resolve_pick(state, suggest()), 'busy')
        state.music.enqueue.assert_not_awaited()
        self.assertEqual(failed_reason(state)['reason'], 'busy')

    async def test_ended_station_gets_no_failure_event(self):
        state = fake_state()
        state.music.enqueue.side_effect = HTTPException(409, 'Station is no longer started')
        with patch.object(dj_picks, 'search_youtube', AsyncMock(return_value=[hit()])):
            self.assertEqual(await resolve_pick(state, suggest()), 'gone')
        self.assertIsNone(failed_reason(state))


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class DjPickRedisTests(unittest.IsolatedAsyncioTestCase):
    """곡 순서 — 신청곡이 먼저고, DJ 선곡은 끼어들지 않는다 (3.4절)."""

    async def asyncSetUp(self):
        self.station = 'st_djpick_' + uuid4().hex
        self.prefix = 'backend:' + self.station
        self.redis = Redis.from_url('redis://127.0.0.1:6379/0', decode_responses=True)
        await self.redis.set(self.prefix + ':station', json.dumps(dict(owner_id='owner', status='started')))

    async def asyncTearDown(self):
        keys = [key async for key in self.redis.scan_iter(self.prefix + ':*')]
        keys.append(f'engine:{self.station}:in')
        await self.redis.delete(*keys)
        await self.redis.aclose()

    async def enqueue(self, key, source='playlist', **extra):
        row = {**segment('music'), 'id': key, 'station_id': self.station, 'music_ref': 'yt:' + key}
        meta = dict(track=dict(track_ref=row['music_ref'], title=key, artist='test', duration_ms=8000),
                    source=source, **extra)
        keys = [self.prefix + ':' + suffix for suffix in
                ('music-submissions', 'segments', 'queue', 'station', 'tracks', 'track-plan')]
        keys += [f'engine:{self.station}:in', self.prefix + ':playlist', self.prefix + ':playlist-order']
        await self.redis.eval(ENQUEUE, 9, *keys, key, json.dumps(row), json.dumps({'segment_id': key}),
                              'owner', json.dumps(meta), self.station, str(time.time()), '', '')

    async def queued(self):
        events = [fields for _, fields in await self.redis.xrange(f'engine:{self.station}:in')]
        return [json.loads(e['payload']) for e in events if e['type'] == 'track.queued']

    async def start(self, key):
        body = dict(segment_id=key, station_id=self.station, at=time.time(),
                    payload=dict(track_ref='yt:' + key, started_at=time.time()))
        await deliver_start(self.redis, self.station, 'evt_start_' + key, json.dumps(body), False)

    async def test_dj_song_becomes_next_when_nothing_is_planned_and_carries_the_suggestion(self):
        await self.enqueue('dj1', 'dj', suggest_id='sg_1', candidate=1)
        self.assertEqual(await self.redis.hget(self.prefix + ':track-plan', 'next'), 'dj1')
        payload = (await self.queued())[0]
        self.assertEqual((payload['source'], payload['suggest_id'], payload['candidate']), ('dj', 'sg_1', 1))
        self.assertEqual(payload['track']['track_ref'], 'yt:dj1')
        self.assertEqual(await self.redis.llen(self.prefix + ':playlist-order'), 0, 'DJ 선곡은 플레이리스트에 넣지 않는다')

    async def test_dj_song_does_not_displace_a_planned_song_but_a_request_does(self):
        await self.enqueue('one')
        await self.enqueue('dj1', 'dj', suggest_id='sg_1', candidate=0)
        self.assertEqual(await self.redis.hget(self.prefix + ':track-plan', 'next'), 'one')
        self.assertEqual([p['track']['track_ref'] for p in await self.queued()], ['yt:one'])
        await self.enqueue('req1', 'request')
        self.assertEqual(await self.redis.hget(self.prefix + ':track-plan', 'next'), 'req1')

    async def test_after_a_song_starts_a_request_goes_before_a_waiting_dj_song(self):
        await self.enqueue('one')
        await self.enqueue('dj1', 'dj', suggest_id='sg_1', candidate=0)
        await self.start('one')
        self.assertEqual(await self.redis.hget(self.prefix + ':track-plan', 'next'), 'dj1', '기다리던 DJ 선곡이 다음 곡이 된다')
        await self.redis.hdel(self.prefix + ':track-plan', 'next')
        await self.enqueue('two')
        await self.enqueue('req1', 'request')
        await self.enqueue('dj2', 'dj', suggest_id='sg_2', candidate=0)  # 신청곡보다 늦게 들어왔다
        await self.redis.hdel(self.prefix + ':track-plan', 'next')
        await self.redis.hset(self.prefix + ':segment-states', 'dj1', json.dumps({'state': 'PLAYED'}))
        await self.start('two')
        self.assertEqual(await self.redis.hget(self.prefix + ':track-plan', 'next'), 'req1', '신청곡이 먼저다')

    async def test_resolve_pick_reports_failure_on_the_engine_stream_once(self):
        music = SimpleNamespace(lock=asyncio.Lock(), enqueue=AsyncMock())
        state = SimpleNamespace(redis=self.redis, settings=SimpleNamespace(station_id=self.station), music=music)
        with patch.object(dj_picks, 'search_youtube', AsyncMock(return_value=[])):
            self.assertEqual(await resolve_pick(state, suggest()), 'not_found')
            self.assertEqual(await resolve_pick(state, suggest()), 'duplicate')
        events = [fields for _, fields in await self.redis.xrange(f'engine:{self.station}:in')]
        self.assertEqual([(e['type'], json.loads(e['payload'])) for e in events],
                         [('track.suggest_failed', {'suggest_id': 'sg_1', 'reason': 'not_found'})])
        await self.redis.delete(f'{self.prefix}:dj-suggest:sg_1')

    async def test_consumer_hands_the_suggestion_to_a_background_task(self):
        """검색과 음원 받기는 몇 분 걸릴 수 있다 — 엔진 이벤트 소비자는 기다리지 않고 넘긴다."""
        await self.redis.set(self.prefix + ':station', json.dumps(dict(owner_id='owner', status='started', created_at=0)))
        settings = Settings('127.0.0.1', 3000, station_id=self.station)
        consumer = EngineConsumer(self.redis, settings)
        await consumer.ensure_group()
        fields = dict(type='track.suggest', contract_version='2', event_id='evt_' + uuid4().hex,
                      station_id=self.station, at=str(time.time()),
                      payload=json.dumps({'suggest_id': 'sg_9', 'candidates': [{'title': '밤편지', 'artist': '아이유'}]}))

        async def deliver():
            entry_id = await self.redis.xadd(consumer.stream, fields)
            await self.redis.xreadgroup(GROUP, consumer.name, {consumer.stream: '>'})
            await consumer.handle(entry_id, fields)

        with patch('app.messaging.consumer.start_pick') as start:
            await deliver()  # 음악 서비스가 없는 소비자(테스트·초기화 전) — 버리지 않고 넘어간다
            start.assert_not_called()
            consumer.state = SimpleNamespace(music=object())
            await deliver()
            start.assert_called_once()
            self.assertEqual(start.call_args.args[1].suggest_id, 'sg_9')
        self.assertEqual(await self.redis.xlen(consumer.stream + ':dead'), 0, '모르는 이벤트로 격리하지 않는다')
        await self.redis.delete(consumer.stream, consumer.stream + ':dead')
