"""비밀번호 무작위 대입 막기: 로그인·비밀번호 변경·탈퇴에서 틀린 횟수를 세고 한도를 넘으면 429."""
import asyncio
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import auth, users
from app.auth import (
    PASSWORD_CLIENT_FAILURE_LIMIT, PASSWORD_FAILURE_LIMIT, PASSWORD_FAILURE_WINDOW_SEC, PasswordGuard, PinGuard,
)
from tests.test_account import PASSWORD, AccountApiTests


def request_from(host, state=None):
    return SimpleNamespace(client=SimpleNamespace(host=host), app=SimpleNamespace(state=state or SimpleNamespace()))


class PasswordGuardTests(unittest.TestCase):
    def blocked(self, guard, request, account):
        try:
            guard.check(request, account)
        except HTTPException as error:
            self.assertEqual(error.status_code, 429)
            self.assertEqual(error.headers['Retry-After'], str(int(PASSWORD_FAILURE_WINDOW_SEC)))
            return True
        return False

    def test_same_client_and_account_is_blocked_then_released_after_the_window(self):
        guard, here = PasswordGuard(), request_from('1.1.1.1')
        with patch.object(auth.time, 'monotonic', return_value=100.0):
            for _ in range(PASSWORD_FAILURE_LIMIT):
                self.assertFalse(self.blocked(guard, here, 'A@example.com'))
                guard.fail(here, 'A@example.com')
            self.assertTrue(self.blocked(guard, here, 'a@EXAMPLE.com'))  # 대소문자만 바꿔도 같은 계정이다
            self.assertFalse(self.blocked(guard, here, 'b@example.com'))
            # 다른 접속지에서 같은 계정은 막지 않는다 — 남이 틀려서 주인의 로그인을 막지 못하게
            self.assertFalse(self.blocked(guard, request_from('2.2.2.2'), 'a@example.com'))
        with patch.object(auth.time, 'monotonic', return_value=100.0 + PASSWORD_FAILURE_WINDOW_SEC + 1):
            self.assertFalse(self.blocked(guard, here, 'a@example.com'))

    def test_client_trying_many_accounts_is_blocked(self):
        guard, here = PasswordGuard(), request_from('1.1.1.1')
        with patch.object(auth.time, 'monotonic', return_value=100.0):
            for index in range(PASSWORD_CLIENT_FAILURE_LIMIT):
                self.assertFalse(self.blocked(guard, here, f'user{index}@example.com'))
                guard.fail(here, f'user{index}@example.com')
            self.assertTrue(self.blocked(guard, here, 'fresh@example.com'))
            self.assertFalse(self.blocked(guard, request_from('2.2.2.2'), 'fresh@example.com'))

    def test_right_password_clears_that_account_only(self):
        guard, here = PasswordGuard(), request_from('1.1.1.1')
        for _ in range(PASSWORD_FAILURE_LIMIT - 1):
            guard.fail(here, 'a@example.com')
        guard.passed(here, 'a@example.com')
        guard.fail(here, 'a@example.com')
        self.assertFalse(self.blocked(guard, here, 'a@example.com'))
        self.assertEqual(len(guard.clients.failures['1.1.1.1']), PASSWORD_FAILURE_LIMIT)

    def test_guard_is_kept_on_the_app_and_empty_keys_are_dropped(self):
        state = SimpleNamespace()
        self.assertIs(PasswordGuard.of(request_from('1.1.1.1', state)), PasswordGuard.of(request_from('2.2.2.2', state)))
        guard = PinGuard()
        self.assertFalse(guard.blocked('never-failed', 1.0))
        self.assertEqual(dict(guard.failures), {})
        # 접속지를 알 수 없는 요청도 센다
        unknown = SimpleNamespace(client=None, app=SimpleNamespace(state=state))
        PasswordGuard.of(unknown).fail(unknown, 'a@example.com')
        self.assertIn('unknown', PasswordGuard.of(unknown).clients.failures)


class HashingTests(unittest.IsolatedAsyncioTestCase):
    async def test_hashing_runs_off_the_event_loop_thread(self):
        seen = []
        real_hash, real_verify = users.hash_password, users.verify_password

        def hashing(password):
            seen.append(threading.current_thread())
            return real_hash(password)

        def verifying(password, hashed):
            seen.append(threading.current_thread())
            return real_verify(password, hashed)

        with patch.object(users, 'hash_password', hashing), patch.object(users, 'verify_password', verifying):
            hashed = await users.hash_password_async('password1234')
            self.assertTrue(await users.verify_password_async('password1234', hashed))
            self.assertFalse(await users.verify_password_async('nope', hashed))
        self.assertEqual(len(seen), 3)
        self.assertNotIn(threading.main_thread(), seen)

    async def test_event_loop_keeps_running_while_a_password_is_checked(self):
        ticks = 0

        async def ticking():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        ticker = asyncio.create_task(ticking())
        try:
            hashed = await users.hash_password_async('password1234')
            before = ticks
            await asyncio.gather(*[users.verify_password_async('password1234', hashed) for _ in range(3)])
        finally:
            ticker.cancel()
        self.assertGreater(ticks, before)  # 같은 스레드에서 돌았다면 그동안 한 번도 못 돈다


class PasswordLimitApiTests(AccountApiTests):
    async def login(self, password, email=None):
        return await self.client.post('/api/auth/login', json={'email': email or self.me['email'], 'password': password})

    async def test_wrong_logins_are_blocked_even_with_the_right_password_afterwards(self):
        for _ in range(PASSWORD_FAILURE_LIMIT):
            self.assertEqual((await self.login('wrong-password')).status_code, 401)
        blocked = await self.login(PASSWORD)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.headers['Retry-After'], str(int(PASSWORD_FAILURE_WINDOW_SEC)))
        # 같은 접속지에서 다른 계정은 로그인된다. 없는 계정도 같은 응답으로 세므로 계정이 있는지 알려 주지 않는다
        self.assertEqual((await self.login(PASSWORD, self.other['email'])).status_code, 200)
        for _ in range(PASSWORD_FAILURE_LIMIT):
            self.assertEqual((await self.login('wrong-password', 'nobody-' + self.tag + '@example.com')).status_code, 401)
        self.assertEqual((await self.login('wrong-password', 'nobody-' + self.tag + '@example.com')).status_code, 429)

    async def test_right_login_resets_the_count(self):
        for _ in range(PASSWORD_FAILURE_LIMIT - 1):
            self.assertEqual((await self.login('wrong-password')).status_code, 401)
        self.assertEqual((await self.login(PASSWORD)).status_code, 200)
        for _ in range(PASSWORD_FAILURE_LIMIT - 1):
            self.assertEqual((await self.login('wrong-password')).status_code, 401)
        self.assertEqual((await self.login(PASSWORD)).status_code, 200)

    async def test_password_checks_inside_the_account_share_the_limit(self):
        change = lambda current: self.client.post('/api/account/password', headers=self.auth(self.me), json={  # noqa: E731
            'currentPassword': current, 'newPassword': 'brand-new-1234'})
        for _ in range(PASSWORD_FAILURE_LIMIT - 1):
            self.assertEqual((await change('wrong-password')).status_code, 403)
        wrong_delete = await self.client.request('DELETE', '/api/account', json={'password': 'wrong-password'},
                                                 headers=self.auth(self.me))
        self.assertEqual(wrong_delete.status_code, 403)
        self.assertEqual((await change(PASSWORD)).status_code, 429)  # 토큰만 가진 사람이 비밀번호를 찍어 맞히지 못한다
        self.assertEqual((await self.login(PASSWORD)).status_code, 429)
        self.assertEqual((await self.client.get('/api/auth/me', headers=self.auth(self.me))).status_code, 200)


# 준비·정리만 빌려 쓴다 — 물려받은 계정 테스트가 여기서 한 번 더 돌지 않게 지운다
for _name in [name for name in vars(AccountApiTests) if name.startswith('test_')]:
    setattr(PasswordLimitApiTests, _name, None)
del AccountApiTests
