from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://aios:aios@localhost:5432/aios",
)

# NullPool: open a fresh asyncpg connection per session and close it on release.
# Avoids reusing a connection bound to a stale asyncio loop (matters for the
# per-test event loops in pytest; negligible cost at this scale).
engine = create_async_engine(DATABASE_URL, future=True, poolclass=NullPool)

Session = async_sessionmaker(engine, expire_on_commit=False)
