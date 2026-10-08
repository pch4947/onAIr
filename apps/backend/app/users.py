"""Redis-backed user accounts. Mirrors the storage pattern used by RequestStore."""
from datetime import datetime, timezone
import json
from uuid import uuid4

import bcrypt


# Atomic so two concurrent signups can't both claim the same email or the same name.
CREATE = """
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then return 'email' end
if redis.call('HEXISTS', KEYS[2], ARGV[2]) == 1 then return 'name' end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
redis.call('HSET', KEYS[2], ARGV[2], ARGV[1])
return 'ok'
"""


class EmailAlreadyExists(ValueError):
    pass


class NameAlreadyExists(ValueError):
    pass


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


class UserStore:
    def __init__(self, redis):
        self.redis = redis
        self.key = "backend:users"
        self.names_key = "backend:usernames"

    async def create(self, email: str, password: str, name: str) -> dict:
        email = email.lower()
        name = name.strip()
        entry = dict(id=str(uuid4()), email=email, name=name, passwordHash=hash_password(password),
                     createdAt=datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        result = await self.redis.eval(CREATE, 2, self.key, self.names_key,
                                        email, name, json.dumps(entry, ensure_ascii=False))
        if result == "email":
            raise EmailAlreadyExists()
        if result == "name":
            raise NameAlreadyExists()
        return entry

    async def get_by_email(self, email: str) -> dict | None:
        raw = await self.redis.hget(self.key, email.lower())
        return json.loads(raw) if raw else None

    async def authenticate(self, email: str, password: str) -> dict | None:
        user = await self.get_by_email(email)
        if user is None or not verify_password(password, user["passwordHash"]):
            return None
        return user
