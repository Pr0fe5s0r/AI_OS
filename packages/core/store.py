from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.schema import Event

# Append-only, month-partitioned events table (created by the migration).
# Postgres is the system of record for event CONTENT; the embedding lives on
# the Neo4j :Event mirror (see core.graph), written by a retryable second job.

_INSERT_EVENT = text(
    """
    INSERT INTO events
        (id, company_id, source, type, actor_id, actor_name, actor_email,
         timestamp, content, metadata, raw, content_tsv, backfilled)
    VALUES
        (:id, :company_id, :source, :type, :actor_id, :actor_name, :actor_email,
         :timestamp, :content, CAST(:metadata AS jsonb), CAST(:raw AS jsonb),
         to_tsvector('english', :content), :backfilled)
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
        -- backfilled is deliberately NOT overwritten: it records how the
        -- event was FIRST ingested, so a re-sync can never quietly relabel
        -- a real live event as backfilled (or vice versa)
    """
)

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
            "backfilled": event.backfilled,
        },
    )


async def get_event(session: AsyncSession, company_id: str, event_id: str) -> Event | None:
    from packages.shared.schema import Actor

    row = (
        await session.execute(
            text(
                """
                SELECT id, company_id, source, type, actor_id, actor_name, actor_email,
                       timestamp, content, metadata, raw, backfilled
                FROM events WHERE company_id = :c AND id = :i LIMIT 1
                """
            ),
            {"c": company_id, "i": event_id},
        )
    ).first()
    if row is None:
        return None
    return _row_to_event(row, Actor)


async def get_events_by_ids(
    session: AsyncSession, company_id: str, event_ids: list[str]
) -> list[Event]:
    """Batch form of get_event — one query, not N.

    The graph mirror stores ids only, so any screen showing graph nodes with
    their real content needs exactly this join back to the system of record.
    """
    from packages.shared.schema import Actor

    if not event_ids:
        return []
    rows = (
        await session.execute(
            text(
                """
                SELECT id, company_id, source, type, actor_id, actor_name, actor_email,
                       timestamp, content, metadata, raw, backfilled
                FROM events WHERE company_id = :c AND id = ANY(:ids)
                """
            ),
            {"c": company_id, "ids": list(event_ids)},
        )
    ).all()
    return [_row_to_event(r, Actor) for r in rows]


def _row_to_event(row: Any, actor_cls: Any) -> Event:
    md = row.metadata if isinstance(row.metadata, dict) else json.loads(row.metadata)
    raw = row.raw if isinstance(row.raw, dict) else json.loads(row.raw)
    return Event(
        id=row.id,
        company_id=row.company_id,
        source=row.source,
        type=row.type,
        actor=actor_cls(id=row.actor_id, name=row.actor_name, email=row.actor_email),
        timestamp=row.timestamp,
        content=row.content,
        metadata=md or {},
        raw=raw or {},
        backfilled=row.backfilled,
    )
