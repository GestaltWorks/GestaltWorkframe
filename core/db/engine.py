"""Async SQLAlchemy engine and session maker.

Backed by SQLite by default at `database.db` in the working directory.
Production VPS deployments override the path via `APP_DATABASE_PATH`; CI and
tests can supply a full `APP_DATABASE_URL` for non-SQLite stores when the
move off SQLite happens.

`get_session` is the FastAPI dependency. It yields a fresh session per
request and closes it when the request ends. The chat stream uses
`async_session_maker()` directly inside its `finally` block because the
request's session is already closed by the time the generator's cleanup
runs.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker


# App session/contact SQLite store. Cloud spillover accounting uses its own
# configurable store in the provider registry.
DEFAULT_SQLITE_PATH = "database.db"


def database_url_from_env() -> str:
    explicit_url = os.getenv("APP_DATABASE_URL", "").strip()
    if explicit_url:
        return explicit_url
    sqlite_path = os.getenv("APP_DATABASE_PATH", DEFAULT_SQLITE_PATH).strip() or DEFAULT_SQLITE_PATH
    return f"sqlite+aiosqlite:///{sqlite_path}"


sqlite_url = database_url_from_env()

engine = create_async_engine(sqlite_url, echo=False)
async_session_maker = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session
