"""Redis-backed user accounts. Mirrors the storage pattern used by RequestStore."""
from datetime import datetime, timezone
import json
import time
from uuid import uuid4

import bcrypt


# Atomic so two concurrent signups can't both claim the same email or the same name.
# 만료된 게스트의 닉네임은 비어 있는 것으로 보고 그 계정을 지운다.
CREATE = """
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then return 'email' end
local holder = redis.call('HGET', KEYS[2], ARGV[2])
local raw = holder and redis.call('HGET', KEYS[1], holder)
if raw then
  local user = cjson.decode(raw)
  if user.kind ~= 'guest' or (user.expiresAt or 0) > tonumber(ARGV[4]) then return 'name' end
  redis.call('HDEL', KEYS[1], holder)
end
redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
redis.call('HSET', KEYS[2], ARGV[2], ARGV[1])
return 'ok'
"""


# 저장된 계정이 게스트일 때만 계정과 닉네임 예약을 함께 지운다 (다른 계정을 지우지 않게).
DELETE_GUEST = """
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return 0 end
local user = cjson.decode(raw)
if user.kind ~= 'guest' then return 0 end
redis.call('HDEL', KEYS[1], ARGV[1])
if redis.call('HGET', KEYS[2], user.name) == ARGV[1] then redis.call('HDEL', KEYS[2], user.name) end
return 1
"""


class EmailAlreadyExists(ValueError):
    pass


class NameAlreadyExists(ValueError):
    pass


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


# 게스트·어드민 계정의 저장 키. .invalid는 예약 도메인이라 EmailStr이 거부해서
# 일반 가입·이메일 로그인과 겹칠 수 없다.
GUEST_DOMAIN = "guest.onair.invalid"
ADMIN_DOMAIN = "admin.onair.invalid"
# 브라우저만 닫고 떠난 게스트도 이 시간이 지나면 닉네임을 돌려준다. 토큰 만료와 같다.
GUEST_TTL_SECONDS = 24 * 3600


class UserStore:
    def __init__(self, redis):
        self.redis = redis
        self.key = "backend:users"
        self.names_key = "backend:usernames"

    async def _insert(self, email: str, name: str, password_hash: str | None, kind: str, **extra) -> dict:
        entry = dict(id=str(uuid4()), email=email, name=name, passwordHash=password_hash, kind=kind,
                     createdAt=datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **extra)
        result = await self.redis.eval(CREATE, 2, self.key, self.names_key,
                                        email, name, json.dumps(entry, ensure_ascii=False), str(time.time()))
        if result == "email":
            raise EmailAlreadyExists()
        if result == "name":
            raise NameAlreadyExists()
        return entry

    async def create(self, email: str, password: str, name: str) -> dict:
        return await self._insert(email.lower(), name.strip(), hash_password(password), "member")

    async def create_guest(self, name: str) -> dict:
        return await self._insert(f"guest-{uuid4().hex}@{GUEST_DOMAIN}", name.strip(), None, "guest",
                                  expiresAt=time.time() + GUEST_TTL_SECONDS)

    async def get_or_create_admin(self, name: str) -> dict:
        email = f"{name.lower()}@{ADMIN_DOMAIN}"
        if existing := await self.get_by_email(email):
            return existing
        try:
            return await self._insert(email, name, None, "admin")
        except EmailAlreadyExists:  # 같은 어드민이 동시에 처음 들어온 경우
            return await self.get_by_email(email)

    async def delete_guest(self, email: str) -> bool:
        return bool(await self.redis.eval(DELETE_GUEST, 2, self.key, self.names_key, email.lower()))

    async def get_by_email(self, email: str) -> dict | None:
        raw = await self.redis.hget(self.key, email.lower())
        return json.loads(raw) if raw else None

    async def authenticate(self, email: str, password: str) -> dict | None:
        user = await self.get_by_email(email)
        # 게스트·어드민 계정은 비밀번호가 없어 이메일 로그인이 안 된다
        if user is None or not user.get("passwordHash") or not verify_password(password, user["passwordHash"]):
            return None
        return user
