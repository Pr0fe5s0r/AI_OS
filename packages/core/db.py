from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://aios:aios@localhost:5432/aios",
)

# How long a connection may sit inside an open transaction doing nothing before
# Postgres closes it.
#
# Measured, painfully: a search cancelled mid-query — which is what happens
# every time a reader closes the answer stream, or the answer deadline fires —
# leaves its asyncpg connection `idle in transaction`. With NullPool nothing
# recycles it, so it stays that way. One such connection, twelve minutes old,
# was enough to make every subsequent search on that collection hang past 150
# seconds; the API logged nothing, the SSE stream simply stopped producing, and
# the browser reported ERR_INCOMPLETE_CHUNKED_ENCODING — "network error" on
# screen, with a healthy database and healthy indexes.
#
# Killing the connection took search from >150s back to 912ms.
#
# It must sit ABOVE the answer deadline, and the first version of this did not.
#
# I set it to two minutes on the reasoning that no query here runs longer than a
# second. That reasoning was wrong about the wrong thing: it is not the QUERIES
# that are long, it is the transaction. An agentic walk holds its session open
# across every model call it makes — measured medians of 35s and 65s, with a
# single run at 80s — and for all of that time the connection is, accurately,
# `idle in transaction`. A 120s ceiling would start killing real answers
# mid-flight, and the caller would see the same "network error" this setting was
# added to prevent.
#
# Ten minutes: comfortably past ANSWER_DEADLINE_SECONDS (300s), so no answer the
# API is still willing to wait for can have its connection pulled; and still
# bounded, so an abandoned transaction self-clears. The leak that caused the
# original outage was twelve minutes old and growing, and this catches it.
IDLE_TX_TIMEOUT_MS = int(os.getenv("IDLE_TX_TIMEOUT_MS", "600000"))

# NullPool: open a fresh asyncpg connection per session and close it on release.
# Avoids reusing a connection bound to a stale asyncio loop (matters for the
# per-test event loops in pytest; negligible cost at this scale).
engine = create_async_engine(
    DATABASE_URL,
    future=True,
    poolclass=NullPool,
    # Set on the connection itself rather than on the server, so it travels
    # with the application and is not one more thing a deployment has to know.
    connect_args={
        "server_settings": {"idle_in_transaction_session_timeout": str(IDLE_TX_TIMEOUT_MS)}
    },
)

Session = async_sessionmaker(engine, expire_on_commit=False)
