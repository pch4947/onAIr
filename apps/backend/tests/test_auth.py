"""Unit tests for JWT issuance and the get_current_user dependency. No real Redis required."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
from fastapi import HTTPException

from app.auth import JWT_ALGORITHM, create_access_token, get_current_user, public_user, token_matches

SECRET = "test-secret-at-least-32-bytes-long!"
USER = {"id": "u1", "email": "test@example.com", "name": "테스터",
        "passwordHash": "irrelevant-here", "createdAt": "2026-01-01T00:00:00.000+00:00"}


def make_request(users_get_by_email=None, redis_error=None):
    users = SimpleNamespace(get_by_email=users_get_by_email or AsyncMock(return_value=USER))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(jwt_secret=SECRET), users=users)))


class TokenTests(unittest.TestCase):
    def test_create_access_token_round_trips_with_expected_claims(self):
        token = create_access_token(USER, SECRET)
        payload = jwt.decode(token, SECRET, algorithms=[JWT_ALGORITHM])
        self.assertEqual(payload["sub"], USER["email"])
        self.assertEqual(payload["uid"], USER["id"])
        self.assertGreater(payload["exp"], payload["iat"])

    def test_token_must_belong_to_this_account_and_postdate_a_password_change(self):
        self.assertTrue(token_matches({"uid": "u1", "iat": 100}, USER))
        self.assertFalse(token_matches({"uid": "u2", "iat": 100}, USER))  # 같은 이메일로 다시 가입한 다른 계정
        self.assertFalse(token_matches({"iat": 100}, USER))
        changed = dict(USER, passwordChangedAt=200.7, keptTokenIat=150)
        cases = [(199, False), (200, True), (201, True), (150, True), (149, False), (None, False), ("200", False)]
        for issued, expected in cases:
            with self.subTest(issued=issued):
                self.assertEqual(token_matches({"uid": "u1", "iat": issued}, changed), expected)

    def test_public_user_strips_password_hash(self):
        pub = public_user(USER)
        self.assertNotIn("passwordHash", pub)
        self.assertEqual(pub["email"], USER["email"])


class GetCurrentUserTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_authorization_header_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await get_current_user(make_request(), authorization=None)
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_non_bearer_scheme_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await get_current_user(make_request(), authorization="Basic abc123")
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_garbage_token_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            await get_current_user(make_request(), authorization="Bearer not-a-real-token")
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_expired_token_is_rejected(self):
        now = datetime.now(timezone.utc)
        expired = jwt.encode({"sub": USER["email"], "uid": USER["id"],
                              "iat": now - timedelta(days=8), "exp": now - timedelta(days=1)},
                             SECRET, algorithm=JWT_ALGORITHM)
        with self.assertRaises(HTTPException) as ctx:
            await get_current_user(make_request(), authorization=f"Bearer {expired}")
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_token_for_deleted_user_is_rejected(self):
        token = create_access_token(USER, SECRET)
        request = make_request(users_get_by_email=AsyncMock(return_value=None))
        with self.assertRaises(HTTPException) as ctx:
            await get_current_user(request, authorization=f"Bearer {token}")
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_valid_token_returns_the_user(self):
        token = create_access_token(USER, SECRET)
        request = make_request()
        with self.assertRaises(HTTPException) as other:  # 이메일은 같지만 다른 계정의 토큰 (#344)
            await get_current_user(request, authorization=f"Bearer {create_access_token(dict(USER, id='u2'), SECRET)}")
        self.assertEqual(other.exception.status_code, 401)
        request = make_request()
        user = await get_current_user(request, authorization=f"Bearer {token}")
        self.assertEqual(user, USER)
        request.app.state.users.get_by_email.assert_awaited_once_with(USER["email"])


if __name__ == "__main__":
    unittest.main()
