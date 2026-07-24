from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# What values does this company ACTUALLY use?
#
# The alternative to a config file saying `default_argument: triage` is not a
# cleverer config file — it is reading the real records. Every label, status,
# priority and assignee a business uses is already sitting in the events we
# ingested; nobody needs to declare them.
#
# Generic by construction, like discovery.py: this reasons about the SHAPE of a
# metadata field (is it a list? a list of objects? a scalar?), never about what
# the field means. The same function learns GitHub labels, Zendesk priorities
# and a warehouse's bin codes.

# Applying a label a repo has never used silently CREATES it on GitHub. So an
# empty result must stay empty — inventing a plausible-looking value is the one
# behaviour this module exists to prevent.


def _values_from(raw: Any, name_key: str | None) -> list[str]:
    """Pull comparable string values out of whatever shape the field holds."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, int | float | bool):
        return [str(raw)]
    if isinstance(raw, dict):
        value = raw.get(name_key) if name_key else None
        return [str(value)] if value else []
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            out.extend(_values_from(item, name_key))
        return out
    return []


async def observed_values(
    session: AsyncSession,
    company_id: str,
    field: str,
    *,
    name_key: str | None = None,
    source: str | None = None,
    event_type: str | None = None,
    sample: int = 500,
) -> list[dict[str, Any]]:
    """The distinct values seen in ``metadata[field]``, most-used first.

    Returns ``[{"value": str, "count": int}, ...]``. Reads a bounded sample of
    the most recent events rather than the whole partitioned table — a
    vocabulary is a current fact, and a label nobody has used in a year is not
    a good suggestion.
    """
    clauses = ["company_id = :c", "metadata ? :field"]
    params: dict[str, Any] = {"c": company_id, "field": field, "limit": sample}
    if source:
        clauses.append("source = :source")
        params["source"] = source
    if event_type:
        clauses.append("type = :etype")
        params["etype"] = event_type

    rows = await session.execute(
        text(
            f"""
            SELECT metadata -> :field AS value FROM events
            WHERE {' AND '.join(clauses)}
            ORDER BY timestamp DESC
            LIMIT :limit
            """
        ),
        params,
    )

    counts: dict[str, int] = {}
    for row in rows:
        for value in _values_from(row.value, name_key):
            counts[value] = counts.get(value, 0) + 1
    return [
        {"value": v, "count": n}
        for v, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
