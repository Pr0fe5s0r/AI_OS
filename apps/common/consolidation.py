from __future__ import annotations

import asyncio
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
    """How often the pass fires.

    The reference implementation cycles every 30s and that is the default, but
    a measured pass took ~54 seconds on a 28-passage collection: on anything
    real the cadence is shorter than the work. Overlapping runs are refused
    outright rather than queued, so a too-short interval degrades into "runs
    back to back" instead of into corruption — but raise it on a large store
    anyway, or most ticks are skips.
    """
    return max(30, int(os.getenv("CONSOLIDATION_INTERVAL_SECONDS", "30")))


# One pass at a time, per process. A measured pass took ~54 seconds on a
# 28-passage collection while the cadence defaults to 30, so runs would overlap
# — and two passes consolidating the same collection at once is not slow, it is
# wrong: both read the same live passages, both write summaries of them, and
# the second archives members the first has already replaced.
_running = asyncio.Lock()


async def consolidate_all(ctx: dict[str, Any]) -> dict[str, Any]:
    """One pass over every collection in every workspace.

    A failure in one collection is recorded against that collection and the
    rest still run — a single unreachable model call must not stop the whole
    store from maintaining itself.
    """
    if not enabled():
        return {"skipped": "CONSOLIDATION_ENABLED is not set"}

    if _running.locked():
        # Skipped rather than queued. These passes are idempotent maintenance,
        # so a missed one costs nothing and the next tick picks the work up;
        # queueing them would build a backlog that never drains.
        return {"skipped": "a consolidation pass is already running"}

    async with _running:
        return await _pass()


async def _pass() -> dict[str, Any]:

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
