"""Run with python -m app from apps/backend."""

import logging

import uvicorn

from .config import Settings, load_settings


def main() -> None:
    settings = load_settings()
    if settings.jwt_secret == Settings.jwt_secret:
        # 기본값은 저장소에 공개돼 있어, 이 값으로 서명하면 누구나 로그인 토큰을 만들 수 있다
        logging.getLogger("uvicorn.error").warning(
            "ONAIR_JWT_SECRET is not set: tokens are signed with the public default. Set it before serving real users.")
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    main()
