from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The work itself — not the alerts about it.
#
# The Feed answers "what needs me?" (situations the watcher engine raised).
# This module answers the other, more basic question a person actually asks
# first: "what have I got?" Without it there is nowhere in the product to
# simply SEE your issues/tickets/orders, which reads as if the data never
# arrived.
#
# Generic by construction. The facets below are (source, type, status) because
# those three exist for ANY connected tool:
#   - source  : which tool it came from            (events.source)
#   - type    : what kind of record it is          (events.type)
#   - status  : where it is in its lifecycle       (metadata[<status_field>])
# ``status_field`` is PROFILE DATA (things.status_field) — "state" for GitHub,
# "status" for a Zendesk/inventory profile — so nothing here knows a tool's
# vocabulary. A profile that declares no status simply gets no status facet.
#
# Unlike the watcher engine, this DOES include backfilled events: history is
# part of "what have I got", even though it must never raise fresh alerts.


def _status_expr(status_field: str | None) -> str:
    """SQL for the status value, or a constant NULL when the profile has no
    status concept (so the query shape stays identical either way)."""
    if not status_field:
        return "NULL"
    # the field name comes from profile data, not a request — but quote it
    # anyway so a stray character can never break out of the JSON path
    return f"metadata->>'{status_field}'"


async def list_items(
    session: AsyncSession,
    company_id: str,
    status_field: str | None = None,
    source: str | None = None,
    type_: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Work items, newest first, filtered by any combination of facets."""
    clauses = ["company_id = :c"]
    params: dict[str, Any] = {"c": company_id, "l": limit}
    if source:
        clauses.append("source = :src")
        params["src"] = source
    if type_:
        clauses.append("type = :typ")
        params["typ"] = type_
    if status and status_field:
        clauses.append(f"{_status_expr(status_field)} = :st")
        params["st"] = status

    rows = await session.execute(
        text(
            f"""
            SELECT id, source, type, actor_name, timestamp, content, metadata, backfilled,
                   {_status_expr(status_field)} AS status
            FROM events
            WHERE {" AND ".join(clauses)}
            ORDER BY timestamp DESC
            LIMIT :l
            """
        ),
        params,
    )

    out: list[dict[str, Any]] = []
    for r in rows:
        md = r.metadata if isinstance(r.metadata, dict) else json.loads(r.metadata)
        content = (r.content or "").strip()
        out.append(
            {
                "id": r.id,
                "source": r.source,
                "type": r.type,
                "status": r.status,
                "title": content.splitlines()[0][:120] if content else r.id,
                "actor": r.actor_name,
                "timestamp": r.timestamp.isoformat(),
                "url": (md or {}).get("url"),
                "backfilled": bool(r.backfilled),
            }
        )
    return out


async def item_facets(
    session: AsyncSession,
    company_id: str,
    status_field: str | None = None,
    source: str | None = None,
    type_: str | None = None,
    status: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Counts for each facet value.

    Proper faceting: every facet's counts reflect the OTHER active filters but
    not its own — so picking "open" doesn't collapse the status counts to just
    "open", and you can still see how many closed items you'd get by switching.
    One grouped query, sliced in Python.
    """
    rows = await session.execute(
        text(
            f"""
            SELECT source, type, {_status_expr(status_field)} AS status, count(*) AS n
            FROM events WHERE company_id = :c
            GROUP BY 1, 2, 3
            """
        ),
        {"c": company_id},
    )
    grouped = [
        {"source": r.source, "type": r.type, "status": r.status, "n": int(r.n)} for r in rows
    ]

    def _count(exclude: str) -> dict[str, int]:
        wanted = {"source": source, "type": type_, "status": status}
        wanted.pop(exclude)
        counts: dict[str, int] = {}
        for g in grouped:
            if any(v is not None and g[k] != v for k, v in wanted.items()):
                continue
            key = g[exclude]
            if key is None:
                continue
            counts[key] = counts.get(key, 0) + g["n"]
        return counts

    def _facet(exclude: str) -> list[dict[str, Any]]:
        return [
            {"key": k, "count": n}
            for k, n in sorted(_count(exclude).items(), key=lambda kv: (-kv[1], kv[0]))
        ]

    return {
        "sources": _facet("source"),
        "types": _facet("type"),
        "statuses": _facet("status") if status_field else [],
    }
