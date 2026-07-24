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
    -- company_id is part of the key: an event id is the SOURCE's id (GitHub
    -- numbers issue_4 in every repo), so without the tenant two workspaces
    -- watching the same repo collide and one quietly overwrites the other.
    ON CONFLICT (company_id, id, timestamp) DO UPDATE SET
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

# Fields whose change is worth remembering, and what to call them to a person.
# Deliberately a small, closed set: metadata carries dozens of keys that churn
# for no reason anyone cares about (etags, comment counts, url variants), and
# logging every one of them would bury the two or three that mean something.
# Names, not paths — the profile decides which metadata key holds "status", so
# the caller passes the mapping in rather than this module guessing.


def _first_line(content: str | None) -> str:
    return (content or "").strip().splitlines()[0][:200] if (content or "").strip() else ""


def _rest(content: str | None) -> str:
    """Everything a record says past its first line, normalized so that
    reformatting alone never reads as a change."""
    lines = (content or "").strip().splitlines()[1:]
    return "\n".join(line.strip() for line in lines if line.strip())[:1000]


async def _record_transitions(
    session: AsyncSession, event: Event, changes: list[tuple[str, Any, Any]]
) -> None:
    """Log what changed about this record since we last saw it.

    Ingest overwrites, so without this a record's history is simply gone: a
    pull request that went open → merged left no trace, and "has this moved?"
    — the question the whole product is built around — could only ever be
    answered about the present.

    The before-value comes from the STORED SNAPSHOT, not from the last
    transition row. Reading it from the transition log looked equivalent and
    was not: on the first scan after this shipped, every existing record had a
    snapshot but no history, so each one logged "status → closed" with an empty
    before — a change nobody made, on ten records at once. The snapshot always
    knows what we previously believed.

    The second condition guards the other direction: two workers handed the
    same payload would otherwise both write the same row. Not a unique index,
    because an issue closed, reopened and closed again really did change three
    times and an index would swallow the second close.
    """
    for field, old_value, new_value in changes:
        await session.execute(
            text(
                """
                INSERT INTO record_transitions
                    (company_id, record_id, source, field, old_value, new_value)
                SELECT :c, :r, :s, :f, :old, :new
                WHERE (
                    SELECT new_value FROM record_transitions
                    WHERE company_id = :c AND record_id = :r AND field = :f
                    ORDER BY observed_at DESC, id DESC LIMIT 1
                ) IS DISTINCT FROM :new
                """
            ),
            {
                "c": event.company_id, "r": event.id, "s": event.source, "f": field,
                "old": None if old_value is None else str(old_value),
                "new": None if new_value is None else str(new_value),
            },
        )


async def store_event(
    session: AsyncSession, event: Event, status_field: str | None = None
) -> None:
    """Append (or refresh) one event, and remember anything that changed.

    ``status_field`` is profile data — GitHub says "state", an inventory
    profile says "status" — so the caller supplies it and this module stays
    free of any tool's vocabulary. Omitted, only the title is watched.
    """
    previous = (
        await session.execute(
            text(
                "SELECT content, metadata FROM events "
                "WHERE company_id = :c AND id = :i LIMIT 1"
            ),
            {"c": event.company_id, "i": event.id},
        )
    ).first()

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

    # Only against a record we had already seen. The first sighting of an issue
    # is not it "changing to open" — logging that would fill the history of a
    # freshly connected workspace with transitions nobody made.
    if previous is None:
        return

    old_meta = previous.metadata if isinstance(previous.metadata, dict) else json.loads(previous.metadata or "{}")
    changes: list[tuple[str, Any, Any]] = []

    def _add(field: str, old: Any, new: Any) -> None:
        if old != new:
            changes.append((field, old, new))

    _add("title", _first_line(previous.content), _first_line(event.content))
    # Everything past the title: a description being edited, and whatever the
    # connector appends about what has happened since — which files a pull
    # request touches, the commit messages on it. Without this a PR that gained
    # a second commit had NO transition at all, so "what changed?" answered
    # "nothing" about work that had visibly moved. A record can change without
    # its status or its name changing; that is the normal case, not the edge.
    _add("detail", _rest(previous.content), _rest(event.content))
    if status_field:
        _add("status", (old_meta or {}).get(status_field), event.metadata.get(status_field))
    if changes:
        await _record_transitions(session, event, changes)


async def list_transitions(
    session: AsyncSession, company_id: str, record_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """What has changed lately — for one record, or across the workspace.

    The counterpart to a snapshot: `events` says what is true now, this says
    what stopped being true and when we noticed.
    """
    clauses = ["company_id = :c"]
    params: dict[str, Any] = {"c": company_id, "l": limit}
    if record_id:
        clauses.append("record_id = :r")
        params["r"] = record_id
    rows = await session.execute(
        text(
            f"""
            SELECT record_id, source, field, old_value, new_value, observed_at
            FROM record_transitions
            WHERE {" AND ".join(clauses)}
            ORDER BY observed_at DESC, id DESC
            LIMIT :l
            """
        ),
        params,
    )
    return [
        {
            "record_id": r.record_id, "source": r.source, "field": r.field,
            "from": r.old_value, "to": r.new_value,
            "observed_at": r.observed_at.isoformat(),
        }
        for r in rows
    ]


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
