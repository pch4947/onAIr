"""Email/password auth: JWT issuance, the current-user dependency, and the auth routes.

No social login here (Google/Naver/Kakao are separate follow-up work).
"""
from datetime import datetime, timedelta, timezone
from typing import Annotated
import asyncio

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from redis.exceptions import RedisError

from .users import EmailAlreadyExists, NameAlreadyExists

router = APIRouter(prefix="/api/auth")
ACCESS_TOKEN_TTL = timedelta(days=7)
JWT_ALGORITHM = "HS256"


class SignupInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=50)


class LoginInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


def create_access_token(user: dict, secret: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": user["email"], "uid": user["id"], "iat": now, "exp": now + ACCESS_TOKEN_TTL}
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def public_user(user: dict) -> dict:
    return {"id": user["id"], "email": user["email"], "name": user["name"], "createdAt": user["createdAt"]}


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


@router.get("/me")
async def me(current_user: Annotated[dict, Depends(get_current_user)]):
    return {"user": public_user(current_user)}
