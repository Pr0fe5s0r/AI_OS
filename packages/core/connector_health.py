from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Generic connector self-monitoring. The core has no idea what "github" means
# here — connector_type is just a string key the caller supplies (in practice
# it's the same value as events.source). Three independent signals feed one
# status, evaluated by the worker cron every 15 minutes:
#   1. consecutive raw-payload schema-validation failures (record_failure)
#   2. field-completeness drift, sampled at ingest time (record_sample)
#   3. ingest volume vs its own learned norm (see core.norms volume baseline)
# The ingest-time hooks (record_success/record_failure/record_sample) only
# ever ACCUMULATE signal — they never set `status`. Only the cron's
# evaluate_connector_health() decides status, so a late-arriving success
# can't race a volume-based "degraded" call into flipping back healthy.

DEGRADED_FAILURES = 3   # consecutive schema failures -> degraded
BROKEN_FAILURES = 10    # consecutive schema failures -> broken
COMPLETENESS_DROP = 0.20  # a field's fast-EMA falling this far below its slow-EMA -> degraded
_FAST_ALPHA = 0.2       # ~5-sample memory: "right now"
_SLOW_ALPHA = 0.02      # ~50-sample memory: "recent history" to compare against


async def _get_row(session: AsyncSession, connector_type: str, company_id: str) -> dict | None:
    row = (
        await session.execute(
            text(
                """
                SELECT connector_type, company_id, last_success_at, consecutive_failures,
                       last_schema_error, events_per_hour_norm, field_completeness, status
                FROM connector_health WHERE connector_type = :t AND company_id = :c
                """
            ),
            {"t": connector_type, "c": company_id},
        )
    ).first()
    if row is None:
        return None
    fc = row.field_completeness if isinstance(row.field_completeness, dict) else json.loads(row.field_completeness)
    return {
        "connector_type": row.connector_type,
        "company_id": row.company_id,
        "last_success_at": row.last_success_at,
        "consecutive_failures": row.consecutive_failures,
        "last_schema_error": row.last_schema_error,
        "events_per_hour_norm": row.events_per_hour_norm,
        "field_completeness": fc or {},
        "status": row.status,
    }


async def _upsert(session: AsyncSession, connector_type: str, company_id: str, **fields: Any) -> None:
    row = await _get_row(session, connector_type, company_id)
    merged = {
        "last_success_at": None,
        "consecutive_failures": 0,
        "last_schema_error": None,
        "events_per_hour_norm": None,
        "field_completeness": {},
        "status": "healthy",
        **(row or {}),
        **fields,
    }
    await session.execute(
        text(
            """
            INSERT INTO connector_health
                (connector_type, company_id, last_success_at, consecutive_failures,
                 last_schema_error, events_per_hour_norm, field_completeness, status, updated_at)
            VALUES
                (:t, :c, :ls, :cf, :err, :norm, CAST(:fc AS jsonb), :st, now())
            ON CONFLICT (connector_type, company_id) DO UPDATE SET
                last_success_at = EXCLUDED.last_success_at,
                consecutive_failures = EXCLUDED.consecutive_failures,
                last_schema_error = EXCLUDED.last_schema_error,
                events_per_hour_norm = EXCLUDED.events_per_hour_norm,
                field_completeness = EXCLUDED.field_completeness,
                status = EXCLUDED.status,
                updated_at = now()
            """
        ),
        {
            "t": connector_type,
            "c": company_id,
            "ls": merged["last_success_at"],
            "cf": merged["consecutive_failures"],
            "err": merged["last_schema_error"],
            "norm": merged["events_per_hour_norm"],
            "fc": json.dumps(merged["field_completeness"]),
            "st": merged["status"],
        },
    )


async def record_success(session: AsyncSession, connector_type: str, company_id: str) -> None:
    """A raw payload passed schema validation. Resets the failure streak.

    A plain reset-to-constant, so concurrent callers can't race each other
    into an inconsistent state the way an increment could.
    """
    await session.execute(
        text(
            """
            INSERT INTO connector_health
                (connector_type, company_id, last_success_at, consecutive_failures,
                 last_schema_error, updated_at)
            VALUES (:t, :c, :now, 0, NULL, now())
            ON CONFLICT (connector_type, company_id) DO UPDATE SET
                last_success_at = EXCLUDED.last_success_at,
                consecutive_failures = 0,
                last_schema_error = NULL,
                updated_at = now()
            """
        ),
        {"t": connector_type, "c": company_id, "now": datetime.now(UTC)},
    )


async def record_failure(session: AsyncSession, connector_type: str, company_id: str, error: str) -> int:
    """A raw payload failed schema validation. Returns the new failure streak.

    Ingestion runs many events through the pipeline concurrently (arq
    processes jobs in parallel), so this must be a single atomic SQL
    increment — a read-then-write in Python here would lose updates under
    concurrent failures (confirmed by hand: 3 concurrent malformed payloads
    only advanced the streak by 1 before this was an atomic UPDATE).
    """
    row = (
        await session.execute(
            text(
                """
                INSERT INTO connector_health
                    (connector_type, company_id, consecutive_failures, last_schema_error, updated_at)
                VALUES (:t, :c, 1, :err, now())
                ON CONFLICT (connector_type, company_id) DO UPDATE SET
                    consecutive_failures = connector_health.consecutive_failures + 1,
                    last_schema_error = EXCLUDED.last_schema_error,
                    updated_at = now()
                RETURNING consecutive_failures
                """
            ),
            {"t": connector_type, "c": company_id, "err": error[:2000]},
        )
    ).one()
    return int(row.consecutive_failures)


async def record_sample(
    session: AsyncSession, connector_type: str, company_id: str, fields_present: dict[str, bool]
) -> None:
    """Roll one sampled event's per-field presence into a fast + slow EMA.

    The fast EMA is "right now"; the slow EMA is its own recent history.
    completeness_drop() compares the two, so no separate history table is
    needed to answer "did this field's population rate just fall off?".

    ``SELECT ... FOR UPDATE`` closes the same concurrent-lost-update window
    record_failure had — two samples landing in the same instant must not
    let one silently overwrite the other's EMA update.
    """
    if not fields_present:
        return
    await session.execute(
        text(
            """
            INSERT INTO connector_health (connector_type, company_id)
            VALUES (:t, :c) ON CONFLICT DO NOTHING
            """
        ),
        {"t": connector_type, "c": company_id},
    )
    row = (
        await session.execute(
            text(
                """
                SELECT field_completeness FROM connector_health
                WHERE connector_type = :t AND company_id = :c FOR UPDATE
                """
            ),
            {"t": connector_type, "c": company_id},
        )
    ).one()
    fc = row.field_completeness if isinstance(row.field_completeness, dict) else json.loads(row.field_completeness)
    fast = dict((fc or {}).get("current") or {})
    slow = dict((fc or {}).get("baseline") or {})
    for field, present in fields_present.items():
        sample = 1.0 if present else 0.0
        fast[field] = sample if field not in fast else (1 - _FAST_ALPHA) * fast[field] + _FAST_ALPHA * sample
        slow[field] = sample if field not in slow else (1 - _SLOW_ALPHA) * slow[field] + _SLOW_ALPHA * sample
    await session.execute(
        text(
            """
            UPDATE connector_health SET field_completeness = CAST(:fc AS jsonb), updated_at = now()
            WHERE connector_type = :t AND company_id = :c
            """
        ),
        {"t": connector_type, "c": company_id, "fc": json.dumps({"current": fast, "baseline": slow})},
    )


def completeness_drop(field_completeness: dict) -> list[str]:
    """Fields whose current (fast) rate has dropped well below their own
    recent history (slow) rate — a live data-quality regression."""
    current = field_completeness.get("current") or {}
    baseline = field_completeness.get("baseline") or {}
    return sorted(
        f for f, rate in current.items()
        if f in baseline and baseline[f] - rate > COMPLETENESS_DROP
    )


async def set_status(
    session: AsyncSession,
    connector_type: str,
    company_id: str,
    status: str,
    events_per_hour_norm: float | None = None,
) -> None:
    """Explicit status set by the health cron, after weighing all signals."""
    fields: dict[str, Any] = {"status": status}
    if events_per_hour_norm is not None:
        fields["events_per_hour_norm"] = events_per_hour_norm
    await _upsert(session, connector_type, company_id, **fields)


async def get_health(session: AsyncSession, company_id: str) -> list[dict]:
    rows = await session.execute(
        text(
            """
            SELECT connector_type, last_success_at, consecutive_failures, last_schema_error,
                   events_per_hour_norm, field_completeness, status, updated_at
            FROM connector_health WHERE company_id = :c ORDER BY connector_type
            """
        ),
        {"c": company_id},
    )
    out = []
    for r in rows:
        fc = r.field_completeness if isinstance(r.field_completeness, dict) else json.loads(r.field_completeness)
        fc = fc or {}
        out.append(
            {
                "connector_type": r.connector_type,
                "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
                "consecutive_failures": r.consecutive_failures,
                "last_schema_error": r.last_schema_error,
                "events_per_hour_norm": r.events_per_hour_norm,
                "field_completeness": {k: round(v, 3) for k, v in (fc.get("current") or {}).items()},
                "status": r.status,
                "updated_at": r.updated_at.isoformat(),
            }
        )
    return out


async def get_health_one(session: AsyncSession, company_id: str, connector_type: str) -> dict | None:
    row = await _get_row(session, connector_type, company_id)
    if row is None:
        return None
    return {**row, "consecutive_failures": row["consecutive_failures"]}
