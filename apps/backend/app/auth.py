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

from .users import EmailAlreadyExists, NameAlreadyExists

router = APIRouter(prefix="/api/auth")
ACCESS_TOKEN_TTL = timedelta(days=7)
JWT_ALGORITHM = "HS256"
PIN_FAILURE_LIMIT = 5
PIN_FAILURE_WINDOW_SEC = 60.0


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

    def __init__(self):
        self.failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, client: str, now: float) -> deque[float]:
        attempts = self.failures[client]
        while attempts and now - attempts[0] > PIN_FAILURE_WINDOW_SEC:
            attempts.popleft()
        return attempts

    def blocked(self, client: str, now: float) -> bool:
        return len(self._recent(client, now)) >= PIN_FAILURE_LIMIT

    def fail(self, client: str, now: float) -> None:
        self._recent(client, now).append(now)


def create_access_token(user: dict, secret: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": user["email"], "uid": user["id"], "iat": now, "exp": now + ACCESS_TOKEN_TTL}
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def public_user(user: dict) -> dict:
    kind = user.get("kind", "member")
    return {"id": user["id"], "email": user["email"], "name": user["name"], "createdAt": user["createdAt"],
            "isGuest": kind == "guest", "isAdmin": kind == "admin"}


def reject_reserved_name(name: str, request: Request) -> None:
    # 어드민 닉네임을 다른 사람이 먼저 가져가면 그 어드민이 참가할 수 없게 된다
    if name.strip() in request.app.state.settings.admin_pins.values():
        raise HTTPException(409, "Name already taken")


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
    try:
        async with asyncio.timeout(3):
            user = await request.app.state.users.authenticate(body.email, body.password)
    except (RedisError, TimeoutError):
        raise HTTPException(503, "Redis unavailable") from None
    if user is None:
        raise HTTPException(401, "Invalid email or password")
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
