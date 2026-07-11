from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.schema import Event

# Append-only, month-partitioned events table (created by the migration).
# store_event() and store_embedding() are separate so an event lands even if
# embedding later fails (the pipeline embeds in a second, retryable job).

_INSERT_EVENT = text(
    """
    INSERT INTO events
        (id, company_id, source, type, actor_id, actor_name, actor_email,
         timestamp, content, metadata, raw, content_tsv)
    VALUES
        (:id, :company_id, :source, :type, :actor_id, :actor_name, :actor_email,
         :timestamp, :content, CAST(:metadata AS jsonb), CAST(:raw AS jsonb),
         to_tsvector('english', :content))
    ON CONFLICT (id, timestamp) DO UPDATE SET
        source      = EXCLUDED.source,
        type        = EXCLUDED.type,
        actor_id    = EXCLUDED.actor_id,
        actor_name  = EXCLUDED.actor_name,
        actor_email = EXCLUDED.actor_email,
        content     = EXCLUDED.content,
        metadata    = EXCLUDED.metadata,
        raw         = EXCLUDED.raw,
        content_tsv = EXCLUDED.content_tsv
    """
)

_INSERT_EMBEDDING = text(
    """
    INSERT INTO event_embeddings (event_id, embedding)
    VALUES (:event_id, CAST(:embedding AS vector))
    ON CONFLICT (event_id) DO UPDATE SET embedding = EXCLUDED.embedding
    """
)


def _to_vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


async def store_event(session: AsyncSession, event: Event) -> None:
    await session.execute(
        _INSERT_EVENT,
        {
            "id": event.id,
            "company_id": event.company_id,
            "source": event.source,
            "type": event.type,
            "actor_id": event.actor.id,
            "actor_name": event.actor.name,
            "actor_email": event.actor.email,
            "timestamp": event.timestamp,
            "content": event.content,
            "metadata": json.dumps(event.metadata),
            "raw": json.dumps(event.raw),
        },
    )


async def store_embedding(
    session: AsyncSession, event_id: str, embedding: list[float]
) -> None:
    await session.execute(
        _INSERT_EMBEDDING,
        {"event_id": event_id, "embedding": _to_vector_literal(embedding)},
    )


async def insert_event(
    session: AsyncSession, event: Event, embedding: list[float]
) -> None:
    """Convenience: store an event and its embedding together (used by tests)."""
    await store_event(session, event)
    await store_embedding(session, event.id, embedding)


async def get_event(session: AsyncSession, company_id: str, event_id: str) -> Event | None:
    from packages.shared.schema import Actor

    row = (
        await session.execute(
            text(
                """
                SELECT id, company_id, source, type, actor_id, actor_name, actor_email,
                       timestamp, content, metadata, raw
                FROM events WHERE company_id = :c AND id = :i LIMIT 1
                """
            ),
            {"c": company_id, "i": event_id},
        )
    ).first()
    if row is None:
        return None
    md = row.metadata if isinstance(row.metadata, dict) else json.loads(row.metadata)
    raw = row.raw if isinstance(row.raw, dict) else json.loads(row.raw)
    return Event(
        id=row.id,
        company_id=row.company_id,
        source=row.source,
        type=row.type,
        actor=Actor(id=row.actor_id, name=row.actor_name, email=row.actor_email),
        timestamp=row.timestamp,
        content=row.content,
        metadata=md or {},
        raw=raw or {},
    )
