import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.auth import get_current_user
from app.config import Settings
from app.main import create_app
from app.music import search_candidates, TrackSearchInput


def track(id='abcdefghijk', **changes):
    return {'info': dict(identifier=id, title='song', author='artist', length=240000,
                         isStream=False, sourceName='youtube') | changes}


class SearchFilterTests(unittest.TestCase):
    def test_filters_live_unknown_long_duplicate_and_untrusted_sources(self):
        valid = track()
        rows = [track(length=600001), track(isStream=True), track(length=0), track(sourceName='soundcloud'),
                track(id='../../x'), valid, valid, track(id='12345678901'), track(id='12345678902'), track(id='12345678903')]
        candidates = search_candidates({'loadType': 'search', 'data': rows}, 600)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(candidates[0].url, 'https://www.youtube.com/watch?v=abcdefghijk')
        self.assertTrue(all(c.thumbnail_url.startswith('https://i.ytimg.com/') for c in candidates))

    def test_empty_and_invalid_service_result_and_query(self):
        self.assertEqual(search_candidates({'loadType': 'empty'}, 600), [])
        with self.assertRaises(ValueError):
            search_candidates({'loadType': 'error', 'data': {'message': 'private'}}, 600)
        for query in [' ', 'x' * 121]:
            with self.assertRaises(ValueError):
                TrackSearchInput(query=query)


class SearchApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_only_search_never_enqueues_and_uses_internal_auth(self):
        app = create_app(Settings('127.0.0.1', 3000, lavalink_password='internal-test'))
        app.state.redis = SimpleNamespace(get=AsyncMock(return_value=json.dumps(dict(owner_id='owner', status='started'))))
        app.state.music = SimpleNamespace(enqueue=AsyncMock())
        path = '/api/stations/st_local_dev/tracks/search'
        upstream = AsyncMock()
        upstream.__aenter__.return_value = upstream
        upstream.get.return_value = httpx.Response(200, json={'loadType': 'search', 'data': [track()]})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            self.assertEqual((await client.post(path, json={'query': 'song'})).status_code, 401)
            app.dependency_overrides[get_current_user] = lambda: {'id': 'other'}
            self.assertEqual((await client.post(path, json={'query': 'song'})).status_code, 403)
            app.dependency_overrides[get_current_user] = lambda: {'id': 'owner'}
            with patch('app.music.httpx.AsyncClient', return_value=upstream) as factory:
                response = await client.post(path, json={'query': '  가수 노래  '})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()['candidates']), 1)
                self.assertEqual(upstream.get.call_args.kwargs['params'], {'identifier': 'ytsearch:가수 노래'})
                self.assertEqual(upstream.get.call_args.kwargs['headers'], {'Authorization': 'internal-test'})
                self.assertFalse(factory.call_args.kwargs['follow_redirects'])
                self.assertNotIn('internal-test', response.text)
                app.state.music.enqueue.assert_not_awaited()
                upstream.get.side_effect = httpx.ReadTimeout('private')
                self.assertEqual((await client.post(path, json={'query': 'song'})).status_code, 504)
                upstream.get.side_effect = None
                upstream.get.return_value = httpx.Response(200, json={'loadType': 'error', 'data': {'message': 'private'}})
                error = await client.post(path, json={'query': 'song'})
                self.assertEqual(error.status_code, 502)
                self.assertNotIn('private', error.text)
