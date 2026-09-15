"""Run with python -m app from apps/backend."""

import uvicorn

from .config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, workers=1)


if __name__ == "__main__":
    main()
