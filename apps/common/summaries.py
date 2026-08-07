"""Make sure every document has a card, and keep it that way.

A card is two or three sentences saying what a whole document is about. It was
a navigation nicety when all it did was enrich the catalogue. It stopped being
optional when retrieval started routing on it: on a store larger than the
catalogue window, the card is what decides which documents a question is even
allowed to see (see ``packages.core.routing``). A document with no card can
still be reached — the passage arm votes too — but it is competing on the
weaker of the two signals.

New uploads get one automatically now that SUMMARIES_ENABLED defaults on. That
leaves everything uploaded BEFORE, which is the store people actually have. So
this pass runs on a cron, finds documents with no card, and queues them.

Three properties it has to have, all learned from the passes that came before:

  it must not pay twice     summarising is one model call per section plus one
                            for the card. The existing "summarize now" endpoint
                            passed force=True for every document every time,
                            re-buying summaries that were already correct — on
                            a hundred-document store, roughly 1,300 calls. This
                            asks the graph what already exists and skips it.

  it must not stampede      a fresh hundred-document store would otherwise
                            enqueue thirteen hundred model calls in one tick.
                            Bounded per pass, so the backfill drains steadily
                            instead of arriving as a thundering herd.

  it must never be the      a summary failure is logged and counted, never
  reason an upload fails    raised. Nothing here may stall ingestion or the API.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from sqlalchemy import text

from packages.core import graph
from packages.core.db import Session
from packages.shared.schema import Lifecycle, Scope

log = logging.getLogger(__name__)

# How many documents one pass may queue. A hundred-document backfill therefore
# takes several ticks and finishes on its own, rather than putting thirteen
# hundred model calls into the queue at once and starving live ingestion.
BATCH = 20

# One pass at a time, per process — identical to consolidation.py and mapper.py.
_running = asyncio.Lock()


def enabled() -> bool:
    """Same switch that gates summaries at ingest. Backfilling documents while
    new uploads are being skipped would be incoherent — either the store keeps
    cards or it does not."""
    return os.getenv("SUMMARIES_ENABLED", "true").lower() in ("1", "true", "yes")


def interval_seconds() -> int:
    """Deliberately slow. This is catch-up work on documents that have been
    sitting there without a card; it is not urgent, and it competes for the
    same provider rate limit that live ingestion needs."""
    try:
        return max(60, int(os.getenv("SUMMARIES_INTERVAL_SECONDS", "300")))
    except ValueError:
        return 300


async def backfill_all(ctx: dict[str, Any]) -> dict[str, Any]:
    """One pass over every collection: queue cards for documents lacking one."""
    if not enabled():
        return {"skipped": "SUMMARIES_ENABLED is off"}
    if _running.locked():
        return {"skipped": "a summaries pass is already running"}

    queue = ctx.get("redis")
    if queue is None:
        return {"skipped": "no queue on this worker"}

    async with _running:
        return await _pass(queue)


async def _pass(queue: Any) -> dict[str, Any]:
    async with Session() as session:
        rows = (
            await session.execute(
                text("SELECT DISTINCT workspace_id, collection_id FROM collections")
            )
        ).all()

    queued = 0
    remaining = 0
    failures: list[dict[str, str]] = []
    for row in rows:
        if queued >= BATCH:
            # Out of budget for this tick. Whatever is left is counted so the
            # backfill's progress is visible rather than guessed at.
            remaining += await _missing_count(
                Scope(workspace_id=row.workspace_id, collection_id=row.collection_id)
            )
            continue
        scope = Scope(workspace_id=row.workspace_id, collection_id=row.collection_id)
        try:
            missing = await missing_cards(scope)
            for item_id in missing[: BATCH - queued]:
                # force=True: this pass has ALREADY established the card is
                # missing, so the job's own gate would only re-check what we
                # just checked. The skip lives here, where it is cheap.
                await queue.enqueue_job(
                    "summarize_item", scope.workspace_id, scope.collection_id, item_id, True
                )
                queued += 1
            remaining += max(0, len(missing) - (BATCH - queued if queued < BATCH else 0))
        except Exception as exc:  # noqa: BLE001 - recorded, never escalated
            log.exception("summary backfill failed for %s", row.collection_id)
            failures.append({"collection": str(row.collection_id), "error": str(exc)[:200]})

    return {
        "collections": len(rows),
        "queued": queued,
        "still_missing": remaining,
        "failures": failures,
    }


async def missing_cards(scope: Scope) -> list[str]:
    """Live documents in this collection with no card, oldest first.

    Oldest first because the backfill exists FOR the old documents — the ones
    uploaded before summaries were switched on. Newest-first would spend the
    early passes on documents that already got a card at ingest.
    """
    async with Session() as session:
        clause = "AND collection_id = :c" if scope.collection_id else ""
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT item_id FROM kb_items
                    WHERE workspace_id = :w AND status = :active {clause}
                    ORDER BY created_at ASC
                    """  # noqa: S608 - clause is a fixed literal, not input
                ),
                {
                    "w": scope.workspace_id,
                    "c": scope.collection_id,
                    "active": str(Lifecycle.ACTIVE),
                },
            )
        ).all()

    have = set(await graph.documents_with_cards(scope))
    return [r.item_id for r in rows if r.item_id not in have]


async def _missing_count(scope: Scope) -> int:
    try:
        return len(await missing_cards(scope))
    except Exception:  # noqa: BLE001 - a count is not worth failing a pass over
        return 0


async def coverage(scope: Scope) -> dict[str, int]:
    """How much of this collection has a card — what the console shows.

    Routing quality is not a thing anyone should have to infer from answer
    quality. If half the store has no card, half the store is competing on the
    weaker signal, and that is a number, not a feeling.
    """
    missing = await missing_cards(scope)
    async with Session() as session:
        clause = "AND collection_id = :c" if scope.collection_id else ""
        total = (
            await session.execute(
                text(
                    f"""
                    SELECT count(*) AS n FROM kb_items
                    WHERE workspace_id = :w AND status = :active {clause}
                    """  # noqa: S608 - clause is a fixed literal, not input
                ),
                {
                    "w": scope.workspace_id,
                    "c": scope.collection_id,
                    "active": str(Lifecycle.ACTIVE),
                },
            )
        ).one()
    return {
        "documents": int(total.n),
        "with_cards": int(total.n) - len(missing),
        "missing": len(missing),
    }
