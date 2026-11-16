"""신청곡 판정 (계약 6.4절, #301): 엔진이 확실히 어울리지 않는다고 한 곡만 거절하고, 나머지는 통과시킨다."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import music, song_fit
from app.auth import get_current_user
from app.config import Settings
from app.main import create_app
from app.song_fit import song_fits
from app.station_runtime import default_runtime

VIDEO = 'BaW_jenozKc'


def engine(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='http://engine')


def answering(status=200, body=None, seen=None):
    def handler(request):
        if seen is not None:
            seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status, text='oops')
    return engine(handler)


async def found(video_id):
    return '[MV] IU(아이유) _ 밤편지', '1theK'


class SongFitsTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_a_judged_misfit_is_rejected(self):
        cases = [({'fit': False, 'judged': True, 'reason': '메탈'}, False),
                 ({'fit': True, 'judged': True, 'reason': None}, True),
                 ({'fit': True, 'judged': False, 'reason': None}, True),
                 ({'fit': False, 'judged': False, 'reason': None}, True),  # 판정하지 못했으면 통과
                 ({'fit': 'false', 'judged': 'true'}, True), ([], True), ({}, True)]
        for body, expected in cases:
            with self.subTest(body=body):
                seen = []
                async with answering(body=body, seen=seen) as client:
                    self.assertEqual(await song_fits(client, 'st_ch01', VIDEO, found), expected)
                self.assertEqual(seen, [('/v1/stations/st_ch01/song-requests/check',
                                         {'title': '[MV] IU(아이유) _ 밤편지', 'artist': '1theK'})])

    async def test_engine_trouble_lets_the_song_through(self):
        for status in (404, 422, 500, 503):
            with self.subTest(status=status):
                async with answering(status=status, body={'fit': False, 'judged': True}) as client:
                    self.assertTrue(await song_fits(client, 'st_ch01', VIDEO, found))
        async with answering() as client:  # 200인데 JSON이 아니다
            self.assertTrue(await song_fits(client, 'st_ch01', VIDEO, found))

        def refuse(request):
            raise httpx.ConnectError('down')

        def slow(request):
            raise httpx.ReadTimeout('slow')

        for handler in (refuse, slow):
            with self.subTest(handler=handler.__name__), self.assertLogs(song_fit.log, 'WARNING'):
                async with engine(handler) as client:
                    self.assertTrue(await song_fits(client, 'st_ch01', VIDEO, found))
        self.assertTrue(await song_fits(None, 'st_ch01', VIDEO, found))  # 엔진 연결이 없다

    async def test_unknown_title_is_not_asked_and_long_fields_are_cut(self):
        seen = []
        async with answering(body={'fit': False, 'judged': True}, seen=seen) as client:
            self.assertTrue(await song_fits(client, 'st_ch01', VIDEO, AsyncMock(return_value=None)))
            self.assertEqual(seen, [])
            self.assertFalse(await song_fits(client, 'st_ch01', VIDEO, AsyncMock(return_value=('가' * 256, ''))))
        self.assertEqual(seen[0][1], {'title': '가' * 200, 'artist': ''})

    async def test_slow_lookup_lets_the_song_through(self):
        async def hang(video_id):
            await asyncio.sleep(5)

        async with answering(body={'fit': False, 'judged': True}) as client:
            with patch.object(song_fit, 'TOTAL_TIMEOUT_SEC', 0.05), self.assertLogs(song_fit.log, 'WARNING'):
                self.assertTrue(await song_fits(client, 'st_ch01', VIDEO, hang))


class SongRequestApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app(Settings('127.0.0.1', 3000, hls_enabled=True))
        self.app.state.redis = SimpleNamespace(get=AsyncMock(return_value=json.dumps({'status': 'started'})))
        self.enqueue = AsyncMock(return_value={'status': 'queued'})
        default_runtime(self.app).music = SimpleNamespace(enqueue=self.enqueue)
        self.app.dependency_overrides[get_current_user] = lambda: {'id': 'listener', 'name': '청취자'}
        self.asked = []
        self.verdict = {'fit': False, 'judged': True, 'reason': '메탈'}
        self.app.state.engine_http = engine(self.answer)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.app), base_url='http://test')
        self.path = '/api/stations/st_local_dev/tracks'
        lookup = patch.object(music, 'fetch_oembed_title', found)
        lookup.start()
        self.addCleanup(lookup.stop)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.app.state.engine_http.aclose()

    def answer(self, request):
        self.asked.append(request.url.path)
        return httpx.Response(200, json=self.verdict)

    async def post(self, **body):
        return await self.client.post(self.path, json={'url': f'https://youtu.be/{VIDEO}', **body},
                                      headers={'Idempotency-Key': 'song-one'})

    async def test_misfit_request_is_rejected_before_the_download(self):
        response = await self.post(source='request', comment='틀어 주세요')
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['detail']['code'], 'song_not_fit')  # 프론트가 이 값으로 가린다
        self.assertNotIn('메탈', response.text)  # 엔진이 쓴 사유는 화면에 내리지 않는다
        self.enqueue.assert_not_awaited()
        self.assertEqual(self.asked, ['/v1/stations/st_local_dev/song-requests/check'])

    async def test_fitting_request_is_queued(self):
        self.verdict = {'fit': True, 'judged': True, 'reason': None}
        self.assertEqual((await self.post(source='request')).status_code, 202)
        self.enqueue.assert_awaited_once_with(VIDEO, 'listener', 'song-one', 'request', None, '청취자', None)

    async def test_playlist_and_vote_songs_are_not_asked(self):
        for body in ({}, {'source': 'vote'}):
            with self.subTest(body=body):
                self.assertEqual((await self.post(**body)).status_code, 202)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.enqueue.await_count, 2)
