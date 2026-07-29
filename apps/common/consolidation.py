from __future__ import annotations

import os
from typing import Any

from sqlalchemy import text

from packages.core import consolidate
from packages.core.db import Session
from packages.shared.schema import Scope

# ---------------------------------------------------------------------------
# The background pass that makes the store self-organising: every collection,
# on a cadence, folds equivalent passages together, drops duplicates, decays
# what has been archived, and re-ranks what is left.
#
# Off by default. It rewrites the retrievable surface — merging replaces
# passages with model-written summaries — and a store that starts editing its
# own contents the moment it is installed, without anyone asking, is not one
# people can reason about. CONSOLIDATION_ENABLED=true turns it on.
# ---------------------------------------------------------------------------


def enabled() -> bool:
    return os.getenv("CONSOLIDATION_ENABLED", "false").lower() in ("1", "true", "yes")


def interval_seconds() -> int:
    """StixDB cycles every 30s. Anything under 30 is refused: each pass makes a
    model call per merge group, and a tighter loop spends money faster than it
    learns anything."""
    return max(30, int(os.getenv("CONSOLIDATION_INTERVAL_SECONDS", "30")))


async def consolidate_all(ctx: dict[str, Any]) -> dict[str, Any]:
    """One pass over every collection in every workspace.

    A failure in one collection is recorded against that collection and the
    rest still run — a single unreachable model call must not stop the whole
    store from maintaining itself.
    """
    if not enabled():
        return {"skipped": "CONSOLIDATION_ENABLED is not set"}

    async with Session() as session:
        rows = (
            await session.execute(
                text("SELECT DISTINCT workspace_id, collection_id FROM collections")
            )
        ).all()

    totals = consolidate.Outcome()
    failures: list[dict[str, str]] = []
    for row in rows:
        scope = Scope(workspace_id=row.workspace_id, collection_id=row.collection_id)
        try:
            async with Session() as session:
                outcome = await consolidate.run_once(session, scope)
            totals.merged += outcome.merged
            totals.deduped += outcome.deduped
            totals.decayed += outcome.decayed
            totals.dropped += outcome.dropped
            totals.promoted += outcome.promoted
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            failures.append({"collection": row.collection_id, "error": str(exc)})
            async with Session() as session:
                await session.execute(
                    text(
                        "INSERT INTO consolidation_runs "
                        "(workspace_id, collection_id, error) VALUES (:w, :c, :e)"
                    ),
                    {"w": row.workspace_id, "c": row.collection_id, "e": str(exc)[:500]},
                )
                await session.commit()

    return {"collections": len(rows), **totals.as_dict(), "failures": failures}


__all__ = ["consolidate_all", "enabled", "interval_seconds"]
