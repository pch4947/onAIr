"""Unit tests for Redis-backed user storage. No real Redis required (mocked client)."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.users import EmailAlreadyExists, NameAlreadyExists, UserStore, hash_password, verify_password


class PasswordHashingTests(unittest.TestCase):
    def test_hash_is_not_plaintext_and_verifies_correctly(self):
        hashed = hash_password("password123")
        self.assertNotEqual(hashed, "password123")
        self.assertTrue(verify_password("password123", hashed))
        self.assertFalse(verify_password("wrong-password", hashed))


class UserStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_lowercases_email_and_stores_hashed_password(self):
        redis = SimpleNamespace(eval=AsyncMock(return_value="ok"))
        store = UserStore(redis)
        user = await store.create("Test@Example.com", "password123", "테스터")
        self.assertEqual(user["email"], "test@example.com")
        self.assertNotIn("password123", user["passwordHash"])
        self.assertTrue(verify_password("password123", user["passwordHash"]))
        redis.eval.assert_awaited_once()
        args = redis.eval.await_args.args
        self.assertEqual(args[2], "backend:users")
        self.assertEqual(args[3], "backend:usernames")
        self.assertEqual(args[4], "test@example.com")
        self.assertEqual(args[5], "테스터")

    async def test_create_trims_name(self):
        redis = SimpleNamespace(eval=AsyncMock(return_value="ok"))
        store = UserStore(redis)
        user = await store.create("test@example.com", "password123", "  테스터  ")
        self.assertEqual(user["name"], "테스터")

    async def test_create_raises_when_email_already_taken(self):
        redis = SimpleNamespace(eval=AsyncMock(return_value="email"))
        store = UserStore(redis)
        with self.assertRaises(EmailAlreadyExists):
            await store.create("test@example.com", "password123", "이름")

    async def test_create_raises_when_name_already_taken(self):
        redis = SimpleNamespace(eval=AsyncMock(return_value="name"))
        store = UserStore(redis)
        with self.assertRaises(NameAlreadyExists):
            await store.create("new@example.com", "password123", "이름")

    async def test_get_by_email_missing_returns_none(self):
        redis = SimpleNamespace(hget=AsyncMock(return_value=None))
        store = UserStore(redis)
        self.assertIsNone(await store.get_by_email("nobody@example.com"))

    async def test_get_by_email_is_case_insensitive(self):
        stored = json.dumps({"id": "u1", "email": "test@example.com", "name": "x",
                             "passwordHash": "h", "createdAt": "2026-01-01T00:00:00.000+00:00"})
        redis = SimpleNamespace(hget=AsyncMock(return_value=stored))
        store = UserStore(redis)
        user = await store.get_by_email("TEST@EXAMPLE.COM")
        self.assertEqual(user["email"], "test@example.com")
        self.assertEqual(redis.hget.await_args.args[1], "test@example.com")

    async def test_authenticate_accepts_correct_and_rejects_wrong_password(self):
        stored = json.dumps({"id": "u1", "email": "test@example.com", "name": "x",
                             "passwordHash": hash_password("password123"),
                             "createdAt": "2026-01-01T00:00:00.000+00:00"})
        redis = SimpleNamespace(hget=AsyncMock(return_value=stored))
        store = UserStore(redis)
        self.assertIsNotNone(await store.authenticate("test@example.com", "password123"))
        self.assertIsNone(await store.authenticate("test@example.com", "wrong-password"))

    async def test_authenticate_unknown_email_returns_none(self):
        redis = SimpleNamespace(hget=AsyncMock(return_value=None))
        store = UserStore(redis)
        self.assertIsNone(await store.authenticate("nobody@example.com", "whatever"))


if __name__ == "__main__":
    unittest.main()
