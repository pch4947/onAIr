"""Demo guest join and admin-pin join. No real Redis required (in-memory fake of the CREATE script)."""
import json
import unittest
from dataclasses import replace

import httpx

from app.config import Settings, parse_admin_pins
from app.main import create_app
from app.users import ADMIN_DOMAIN, GUEST_DOMAIN, UserStore


class FakeRedis:
    """users.CREATE 스크립트와 hget만 흉내 낸다."""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}

    async def eval(self, _script, _numkeys, users_key, names_key, email, name, entry):
        users, names = self.hashes.setdefault(users_key, {}), self.hashes.setdefault(names_key, {})
        if email in users:
            return 'email'
        if name in names:
            return 'name'
        users[email], names[name] = entry, email
        return 'ok'

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)


def make_app(pins='0808:ADMIN1,0707:ADMIN2'):
    app = create_app(replace(Settings('127.0.0.1', 3000), admin_pins=parse_admin_pins(pins)))
    app.state.users = UserStore(FakeRedis())
    return app


class AdminPinConfigTests(unittest.TestCase):
    def test_parses_and_rejects_bad_values(self):
        self.assertEqual(parse_admin_pins('0808:ADMIN1, 0707:ADMIN2'), {'0808': 'ADMIN1', '0707': 'ADMIN2'})
        self.assertEqual(parse_admin_pins(''), {})
        for raw in ['0808', '08:ADMIN1', '0808:관리자', '0808:A,0808:B', '0808:A,0707:A']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_admin_pins(raw)

    def test_pins_are_hidden_from_repr(self):
        settings = replace(Settings('127.0.0.1', 3000), admin_pins={'0808': 'ADMIN1'})
        self.assertNotIn('0808', repr(settings))


class DemoJoinApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_join_issues_token_and_rejects_duplicates(self):
        app = make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            created = await client.post('/api/auth/guest', json={'name': '  청취자1 '})
            self.assertEqual(created.status_code, 201)
            user = created.json()['user']
            self.assertEqual(user['name'], '청취자1')
            self.assertTrue(user['isGuest'])
            self.assertFalse(user['isAdmin'])
            self.assertTrue(user['email'].endswith('@' + GUEST_DOMAIN))
            me = await client.get('/api/auth/me', headers={'Authorization': f"Bearer {created.json()['accessToken']}"})
            self.assertEqual(me.json()['user']['id'], user['id'])
            self.assertEqual((await client.post('/api/auth/guest', json={'name': '청취자1'})).status_code, 409)
            for body in [{'name': ''}, {'name': '   '}, {'name': 'x' * 21}]:
                with self.subTest(body=body):
                    self.assertEqual((await client.post('/api/auth/guest', json=body)).status_code, 422)

    async def test_admin_names_are_reserved_for_guests_and_signup(self):
        app = make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            self.assertEqual((await client.post('/api/auth/guest', json={'name': 'ADMIN1'})).status_code, 409)
            signup = await client.post('/api/auth/signup', json={
                'email': 'someone@example.com', 'password': 'password123', 'name': 'ADMIN2'})
            self.assertEqual(signup.status_code, 409)

    async def test_admin_pin_join_reuses_the_same_admin_account(self):
        app = make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            first = await client.post('/api/auth/admin', json={'pin': '0808'})
            self.assertEqual(first.status_code, 200)
            user = first.json()['user']
            self.assertEqual(user['name'], 'ADMIN1')
            self.assertTrue(user['isAdmin'])
            self.assertEqual(user['email'], 'admin1@' + ADMIN_DOMAIN)
            again = await client.post('/api/auth/admin', json={'pin': '0808'})
            self.assertEqual(again.json()['user']['id'], user['id'])
            self.assertEqual((await client.post('/api/auth/admin', json={'pin': '0707'})).json()['user']['name'], 'ADMIN2')

    async def test_wrong_pins_are_rejected_then_blocked(self):
        app = make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            for _ in range(5):
                self.assertEqual((await client.post('/api/auth/admin', json={'pin': '1234'})).status_code, 401)
            blocked = await client.post('/api/auth/admin', json={'pin': '0808'})
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(blocked.headers['Retry-After'], '60')

    async def test_admin_join_is_off_without_pins(self):
        app = make_app(pins='')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            self.assertEqual((await client.post('/api/auth/admin', json={'pin': '0808'})).status_code, 404)

    async def test_guest_and_admin_accounts_cannot_use_email_login(self):
        app = make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            guest = (await client.post('/api/auth/guest', json={'name': '게스트'})).json()['user']
            await client.post('/api/auth/admin', json={'pin': '0808'})
            for email in [guest['email'], 'admin1@' + ADMIN_DOMAIN]:
                with self.subTest(email=email):
                    # .invalid 도메인은 EmailStr이 거부하므로 비밀번호 검사 전에 422
                    self.assertEqual((await client.post('/api/auth/login', json={
                        'email': email, 'password': 'anything'})).status_code, 422)
        stored = json.loads(app.state.users.redis.hashes['backend:users'][guest['email']])
        self.assertIsNone(stored['passwordHash'])
        self.assertIsNone(await app.state.users.authenticate(guest['email'], 'anything'))


if __name__ == '__main__':
    unittest.main()
