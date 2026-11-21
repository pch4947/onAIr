"""Auth: email/password, demo guest and admin-pin join, JWT issuance, and the current-user dependency."""
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Annotated
import asyncio
import hmac
import time

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from redis.exceptions import RedisError

from .users import WITHDRAWN_NAME, EmailAlreadyExists, NameAlreadyExists

router = APIRouter(prefix="/api/auth")
ACCESS_TOKEN_TTL = timedelta(days=7)
JWT_ALGORITHM = "HS256"
PIN_FAILURE_LIMIT = 5
PIN_FAILURE_WINDOW_SEC = 60.0
# 비밀번호 무작위 대입 막기. 같은 접속지에서 같은 계정을 틀리면 금방 막고, 계정을 바꿔 가며 틀리는 접속지도 막는다.
# 계정만으로는 막지 않는다 — 남이 일부러 틀려서 주인의 로그인을 막을 수 있기 때문이다
PASSWORD_FAILURE_LIMIT = 5
PASSWORD_CLIENT_FAILURE_LIMIT = 30
PASSWORD_FAILURE_WINDOW_SEC = 300.0


class SignupInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=50)


class LoginInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class GuestInput(BaseModel):
    name: str = Field(min_length=1, max_length=20, pattern=r"\S")


class AdminInput(BaseModel):
    pin: str = Field(min_length=1, max_length=16)


class PinGuard:
    """접속지별 최근 실패 시각. 창 안에서 한도를 넘으면 핀을 확인하지 않고 거부한다."""

    def __init__(self, limit: int = PIN_FAILURE_LIMIT, window: float = PIN_FAILURE_WINDOW_SEC):
        self.failures: dict[str, deque[float]] = defaultdict(deque)
        self.limit, self.window = limit, window

    def _recent(self, client: str, now: float) -> deque[float]:
        attempts = self.failures[client]
        while attempts and now - attempts[0] > self.window:
            attempts.popleft()
        return attempts

    def blocked(self, client: str, now: float) -> bool:
        blocked = len(self._recent(client, now)) >= self.limit
        if not self.failures[client]:
            del self.failures[client]  # 실패가 없는 키를 쌓아 두지 않는다
        return blocked

    def fail(self, client: str, now: float) -> None:
        self._recent(client, now).append(now)

    def clear(self, client: str) -> None:
        self.failures.pop(client, None)


class PasswordGuard:
    """비밀번호 확인 실패를 센다 (로그인, 비밀번호 변경, 탈퇴). 한도를 넘으면 비밀번호를 확인하지 않고 429."""

    def __init__(self):
        self.pairs = PinGuard(PASSWORD_FAILURE_LIMIT, PASSWORD_FAILURE_WINDOW_SEC)
        self.clients = PinGuard(PASSWORD_CLIENT_FAILURE_LIMIT, PASSWORD_FAILURE_WINDOW_SEC)

    @staticmethod
    def of(request: Request) -> "PasswordGuard":
        guard = getattr(request.app.state, "password_guard", None)
        if guard is None:
            guard = request.app.state.password_guard = PasswordGuard()
        return guard

    @staticmethod
    def _keys(request: Request, account: str) -> tuple[str, str]:
        client = request.client.host if request.client else "unknown"
        return client, f"{client}|{account.lower()}"

    def check(self, request: Request, account: str) -> None:
        client, pair = self._keys(request, account)
        now = time.monotonic()
        if self.pairs.blocked(pair, now) or self.clients.blocked(client, now):
            raise HTTPException(429, "Too many attempts",
                                headers={"Retry-After": str(int(PASSWORD_FAILURE_WINDOW_SEC))})

    def fail(self, request: Request, account: str) -> None:
        client, pair = self._keys(request, account)
        now = time.monotonic()
        self.pairs.fail(pair, now)
        self.clients.fail(client, now)

    def passed(self, request: Request, account: str) -> None:
        # 맞게 넣었으면 그 계정의 실패 기록은 지운다. 접속지 전체의 기록은 남긴다
        self.pairs.clear(self._keys(request, account)[1])


def create_access_token(user: dict, secret: str) -> str:
    now = datetime.now(timezone.utc)
    expires = datetime.fromtimestamp(user["expiresAt"], timezone.utc) if "expiresAt" in user else now + ACCESS_TOKEN_TTL
    payload = {"sub": user["email"], "uid": user["id"], "iat": now, "exp": expires}
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def public_user(user: dict) -> dict:
    kind = user.get("kind", "member")
    return {"id": user["id"], "email": user["email"], "name": user["name"], "createdAt": user["createdAt"],
            "isGuest": kind == "guest", "isAdmin": kind == "admin",
            "defaultAnonymous": bool(user.get("defaultAnonymous", False))}


def reject_reserved_name(name: str, request: Request) -> None:
    # 어드민 닉네임을 다른 사람이 먼저 가져가면 그 어드민이 참가할 수 없게 된다
    if name.strip() == WITHDRAWN_NAME or name.strip() in request.app.state.settings.admin_pins.values():
        raise HTTPException(409, "Name already taken")


def token_matches(payload: dict, user: dict) -> bool:
    """토큰이 지금 이 계정의 것인가 (#344). 이메일만 보면 탈퇴한 뒤 같은 이메일로 다시 가입한 계정에 옛 토큰이 통한다.
    비밀번호를 바꾸면 그 전에 발급된 토큰은 버린다 — 바꾼 요청에 쓴 토큰(keptTokenIat)만 남긴다."""
    if payload.get("uid") != user["id"]:
        return False
    changed = user.get("passwordChangedAt")
    if changed is None:
        return True
    issued = payload.get("iat")
    # iat는 초 단위라, 바꾼 바로 그 초에 새로 로그인한 토큰까지 받는다
    return isinstance(issued, (int, float)) and (issued >= int(changed) or issued == user.get("keptTokenIat"))


async def get_current_user(request: Request,
                           authorization: Annotated[str | None, Header()] = None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(token, request.app.state.settings.jwt_secret, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token") from None
    email = payload.get("sub")
    if not email:
        raise HTTPException(401, "Invalid token payload")
    try:
        async with asyncio.timeout(3):
            user = await request.app.state.users.get_by_email(email)
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    if user is None:
        raise HTTPException(401, "User not found")
    if not token_matches(payload, user):
        raise HTTPException(401, "Invalid or expired token")
    return user


async def get_broadcast_host(user: Annotated[dict, Depends(get_current_user)]) -> dict:
    """게스트는 청취·입력·게시판까지만 쓴다. 일반 회원과 어드민은 방송을 만들 수 있다."""
    if user.get("kind") == "guest":
        raise HTTPException(403, "Guests cannot create broadcasts")
    return user


@router.post("/signup", status_code=201)
async def signup(body: SignupInput, request: Request):
    reject_reserved_name(body.name, request)
    try:
        async with asyncio.timeout(3):
            user = await request.app.state.users.create(body.email, body.password, body.name)
    except EmailAlreadyExists:
        raise HTTPException(409, "Email already registered") from None
    except NameAlreadyExists:
        raise HTTPException(409, "Name already taken") from None
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    token = create_access_token(user, request.app.state.settings.jwt_secret)
    return {"user": public_user(user), "accessToken": token}


@router.post("/login")
async def login(body: LoginInput, request: Request):
    guard = PasswordGuard.of(request)
    guard.check(request, body.email)
    try:
        async with asyncio.timeout(3):
            user = await request.app.state.users.authenticate(body.email, body.password)
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    if user is None:
        guard.fail(request, body.email)
        raise HTTPException(401, "Invalid email or password")
    guard.passed(request, body.email)
    token = create_access_token(user, request.app.state.settings.jwt_secret)
    return {"user": public_user(user), "accessToken": token}


@router.post("/guest", status_code=201)
async def join_as_guest(body: GuestInput, request: Request):
    reject_reserved_name(body.name, request)
    try:
        async with asyncio.timeout(3):
            user = await request.app.state.users.create_guest(body.name)
    except NameAlreadyExists:
        raise HTTPException(409, "Name already taken") from None
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    token = create_access_token(user, request.app.state.settings.jwt_secret)
    return {"user": public_user(user), "accessToken": token}


@router.post("/admin")
async def join_as_admin(body: AdminInput, request: Request):
    pins = request.app.state.settings.admin_pins
    if not pins:
        raise HTTPException(404, "Admin join is not configured")
    guard = getattr(request.app.state, "pin_guard", None)
    if guard is None:
        guard = request.app.state.pin_guard = PinGuard()
    client, now = request.client.host if request.client else "unknown", time.monotonic()
    if guard.blocked(client, now):
        raise HTTPException(429, "Too many attempts", headers={"Retry-After": str(int(PIN_FAILURE_WINDOW_SEC))})
    name = next((admin for pin, admin in pins.items() if hmac.compare_digest(pin, body.pin)), None)
    if name is None:
        guard.fail(client, now)
        raise HTTPException(401, "Invalid pin")
    try:
        async with asyncio.timeout(3):
            user = await request.app.state.users.get_or_create_admin(name)
    except NameAlreadyExists:
        raise HTTPException(409, "Admin name already taken by another account") from None
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    token = create_access_token(user, request.app.state.settings.jwt_secret)
    return {"user": public_user(user), "accessToken": token}


@router.get("/me")
async def me(current_user: Annotated[dict, Depends(get_current_user)]):
    return {"user": public_user(current_user)}


@router.post("/logout", status_code=204)
async def logout(request: Request, current_user: Annotated[dict, Depends(get_current_user)]):
    """JWT는 서버에 남지 않으므로 일반 계정은 할 일이 없다. 게스트 계정은 지워 닉네임도 다시 쓸 수 있게 한다."""
    if current_user.get("kind") != "guest":
        return
    try:
        async with asyncio.timeout(3):
            await request.app.state.users.delete_guest(current_user["email"])
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
