"""Demo guest join and admin-pin join. No real Redis required (in-memory fake of the CREATE script)."""
import json
import os
import time
import unittest
from dataclasses import replace
from uuid import uuid4

import httpx
import jwt
from redis.asyncio import Redis

from app.config import Settings, parse_admin_pins
from app.main import create_app
from app.users import ADMIN_DOMAIN, CREATE, DELETE_GUEST, GUEST_DOMAIN, GUEST_TTL_SECONDS, NameAlreadyExists, UserStore


class FakeRedis:
    """users.CREATE·DELETE_GUEST 스크립트와 hget만 흉내 낸다."""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}

    async def eval(self, script, _numkeys, users_key, names_key, email, *args):
        users, names = self.hashes.setdefault(users_key, {}), self.hashes.setdefault(names_key, {})
        if script == DELETE_GUEST:
            raw = users.get(email)
            if raw is None or json.loads(raw).get('kind') != 'guest':
                return 0
            name = json.loads(users.pop(email))['name']
            if names.get(name) == email:
                del names[name]
            return 1
        assert script == CREATE
        name, entry, now = args
        if email in users:
            return 'email'
        holder = json.loads(users[names[name]]) if name in names else None
        if holder:
            if holder.get('kind') != 'guest' or holder.get('expiresAt', 0) > float(now):
                return 'name'
            del users[names[name]]
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


class GuestExpiryApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_token_ends_with_the_guest(self):
        app = make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            created = (await client.post('/api/auth/guest', json={'name': '하루손님'})).json()
            admin = (await client.post('/api/auth/admin', json={'pin': '0808'})).json()
        for token, lifetime in ((created['accessToken'], GUEST_TTL_SECONDS), (admin['accessToken'], 7 * 24 * 3600)):
            claims = jwt.decode(token, app.state.settings.jwt_secret, algorithms=['HS256'])
            self.assertAlmostEqual(claims['exp'] - time.time(), lifetime, delta=5)
        self.assertNotIn('expiresAt', created['user'])

    async def test_expired_guest_gives_the_name_back_and_loses_the_account(self):
        app = make_app()
        users = app.state.users.redis.hashes
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            old = (await client.post('/api/auth/guest', json={'name': '떠난손님'})).json()
            self.assertEqual((await client.post('/api/auth/guest', json={'name': '떠난손님'})).status_code, 409)
            record = json.loads(users['backend:users'][old['user']['email']])
            users['backend:users'][old['user']['email']] = json.dumps(record | {'expiresAt': time.time() - 1})
            new = await client.post('/api/auth/guest', json={'name': '떠난손님'})
            self.assertEqual(new.status_code, 201)
            self.assertNotEqual(new.json()['user']['id'], old['user']['id'])
            stale = {'Authorization': f"Bearer {old['accessToken']}"}
            self.assertEqual((await client.get('/api/auth/me', headers=stale)).status_code, 401)


@unittest.skipUnless(os.environ.get('ONAIR_INTEGRATION_TEST') == '1', 'Requires Redis')
class GuestNameScriptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = Redis.from_url('redis://127.0.0.1:6379/0', decode_responses=True)
        self.store = UserStore(self.redis)
        self.names = ['이름' + uuid4().hex[:8] for _ in range(4)]
        self.emails = []

    async def asyncTearDown(self):
        await self.redis.hdel(self.store.names_key, *self.names)
        await self.redis.hdel(self.store.key, *self.emails)
        await self.redis.aclose()

    async def expire(self, user):
        await self.redis.hset(self.store.key, user['email'], json.dumps(user | {'expiresAt': time.time() - 1}, ensure_ascii=False))

    async def test_active_guest_keeps_the_name_and_expired_guest_releases_it(self):
        first = await self.store.create_guest(self.names[0])
        self.emails.append(first['email'])
        with self.assertRaises(NameAlreadyExists):
            await self.store.create_guest(self.names[0])
        await self.expire(first)
        second = await self.store.create_guest(self.names[0])
        self.emails.append(second['email'])
        self.assertIsNone(await self.store.get_by_email(first['email']))
        self.assertEqual(await self.redis.hget(self.store.names_key, self.names[0]), second['email'])

    async def test_a_guest_without_expiry_is_treated_as_expired(self):
        first = await self.store.create_guest(self.names[1])
        self.emails.append(first['email'])
        await self.redis.hset(self.store.key, first['email'], json.dumps(
            {k: v for k, v in first.items() if k != 'expiresAt'}, ensure_ascii=False))
        second = await self.store.create_guest(self.names[1])
        self.emails.append(second['email'])
        self.assertIsNone(await self.store.get_by_email(first['email']))

    async def test_member_name_is_never_released(self):
        email = f'{uuid4().hex}@example.com'
        self.emails.append(email)
        await self.store.create(email, 'password123', self.names[2])
        with self.assertRaises(NameAlreadyExists):
            await self.store.create_guest(self.names[2])
        self.assertIsNotNone(await self.store.get_by_email(email))

    async def test_expired_guest_name_opens_to_signup(self):
        guest = await self.store.create_guest(self.names[3])
        await self.expire(guest)
        email = f'{uuid4().hex}@example.com'
        self.emails.append(email)
        await self.store.create(email, 'password123', self.names[3])
        self.assertEqual(await self.redis.hget(self.store.names_key, self.names[3]), email)
        self.assertIsNone(await self.store.get_by_email(guest['email']))


class LogoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_logout_deletes_account_and_frees_nickname(self):
        app = make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            token = (await client.post('/api/auth/guest', json={'name': '잠깐손님'})).json()['accessToken']
            auth = {'Authorization': f'Bearer {token}'}
            self.assertEqual((await client.post('/api/auth/logout', headers=auth)).status_code, 204)
            self.assertEqual((await client.get('/api/auth/me', headers=auth)).status_code, 401)
            self.assertEqual((await client.post('/api/auth/guest', json={'name': '잠깐손님'})).status_code, 201)

    async def test_member_and_admin_logout_keep_the_account(self):
        app = make_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            member = (await client.post('/api/auth/signup', json={
                'email': 'member@example.com', 'password': 'password123', 'name': '회원'})).json()['accessToken']
            admin = (await client.post('/api/auth/admin', json={'pin': '0808'})).json()['accessToken']
            for token in (member, admin):
                auth = {'Authorization': f'Bearer {token}'}
                self.assertEqual((await client.post('/api/auth/logout', headers=auth)).status_code, 204)
                self.assertEqual((await client.get('/api/auth/me', headers=auth)).status_code, 200)
            self.assertEqual((await client.post('/api/auth/logout')).status_code, 401)


if __name__ == '__main__':
    unittest.main()
