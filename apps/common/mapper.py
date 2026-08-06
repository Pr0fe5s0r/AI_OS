from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any

from sqlalchemy import text

from packages.core import graph
from packages.core.db import Session
from packages.core.summarize import summary_chunk_id, write_summary
from packages.shared.schema import Scope

# ---------------------------------------------------------------------------
# The probe-mapper — background pass that guarantees every chunk eventually
# sits under a summary.
#
# One run:
#   1. graph.unmapped_chunks(scope) → find passages no summary covers yet.
#   2. Pick a random unmapped chunk; generate a probe question from its text.
#   3. search.search_traced(question) → surfaced chunks.
#   4. Unmapped ones among them form a cluster; LLM writes a section_summary.
#   5. graph.upsert_index_summary + store probe_question.
#   6. Insert a mapper_runs row for the UI feed.
#
# Gated by MAPPER_ENABLED. A separate flag from SUMMARIES_ENABLED because the
# mapper is a recurring background cost while summaries at ingest are one-shot.
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


def mapper_enabled() -> bool:
    return os.getenv("MAPPER_ENABLED", "false").lower() in ("1", "true", "yes")


def mapper_interval() -> int:
    """Seconds between mapper ticks. Default 300 (5 minutes)."""
    return max(60, int(os.getenv("MAPPER_INTERVAL_SECONDS", "300")))


# One pass at a time, per process — identical to consolidation.py:47.
_running = asyncio.Lock()


async def map_unmapped_all(ctx: dict[str, Any]) -> dict[str, Any]:
    """One mapper pass over every collection in every workspace.

    Skipped if disabled, or if another pass is still running. A missed tick
    costs nothing — the next one picks the work up.
    """
    if not mapper_enabled():
        return {"skipped": "MAPPER_ENABLED is not set"}

    if _running.locked():
        return {"skipped": "a mapper pass is already running"}

    async with _running:
        return await _pass()


async def _pass() -> dict[str, Any]:
    async with Session() as session:
        rows = (
            await session.execute(
                text("SELECT DISTINCT workspace_id, collection_id FROM collections")
            )
        ).all()

    total_mapped = 0
    failures: list[dict[str, str]] = []
    for row in rows:
        scope = Scope(workspace_id=row.workspace_id, collection_id=row.collection_id)
        try:
            mapped = await _map_collection(scope)
            total_mapped += mapped
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            failures.append({"collection": row.collection_id, "error": str(exc)})
            async with Session() as session:
                await session.execute(
                    text(
                        "INSERT INTO mapper_runs "
                        "(workspace_id, collection_id, error) VALUES (:w, :c, :e)"
                    ),
                    {"w": row.workspace_id, "c": row.collection_id, "e": str(exc)[:500]},
                )
                await session.commit()

    return {"collections": len(rows), "chunks_mapped": total_mapped, "failures": failures}


async def _map_collection(scope: Scope) -> int:
    """Try to map one cluster of unmapped chunks in a single collection."""
    unmapped = await graph.unmapped_chunks(scope, limit=200)
    if not unmapped:
        return 0

    # Pick a random unmapped chunk as the seed for this pass.
    seed = random.choice(unmapped)  # noqa: S311 - not security-sensitive
    seed_id: str = seed["chunk_id"]

    # Read the seed's text from Postgres.
    async with Session() as session:
        row = (
            await session.execute(
                text(
                    "SELECT text, heading FROM kb_chunks "
                    "WHERE chunk_id = :cid AND workspace_id = :w"
                ),
                {"cid": seed_id, "w": scope.workspace_id},
            )
        ).first()
    if row is None:
        return 0

    seed_text: str = row.text or ""
    seed_heading: str = row.heading or ""

    # Generate a probe question from the seed content.
    from packages.core.llm import chat

    try:
        # On a thread: a blocking model call must not stall the worker loop.
        reply = await asyncio.to_thread(
            chat,
            [
                {
                    "role": "user",
                    "content": (
                        "Write a short factual question that someone might ask about "
                        "this passage. Reply with the question only, nothing else.\n\n"
                        f"PASSAGE:\n{seed_text[:2000]}"
                    ),
                }
            ],
        )
        question = reply.strip()
    except Exception:
        question = seed_heading or seed_text[:100]

    if not question:
        question = seed_heading or seed_text[:100]

    # Search with the probe question to find related chunks.
    from packages.core import search

    try:
        async with Session() as session:
            hits, _trace = await search.search_traced(
                session, scope, question
            )
    except Exception:
        log.exception("mapper search failed for scope %s", scope)
        return 0

    # Identify which surfaced chunks are unmapped. A Hit rolls up a document
    # and carries the passages that matched under `.passages`; their chunk_ids
    # are what the mapper brings under a summary — a Hit has no chunk_id of its
    # own, so reading one would leave every cluster as just the seed.
    unmapped_ids = {u["chunk_id"] for u in unmapped}
    cluster_ids: list[str] = [seed_id]  # always include the seed
    for h in hits:
        for passage in getattr(h, "passages", None) or []:
            cid = passage.chunk_id
            if cid in unmapped_ids and cid not in cluster_ids:
                cluster_ids.append(cid)

    if not cluster_ids:
        return 0

    # Read the cluster's texts from Postgres.
    async with Session() as session:
        placeholders = ", ".join(f":c{i}" for i in range(len(cluster_ids)))
        params: dict[str, Any] = {"w": scope.workspace_id}
        params.update({f"c{i}": cid for i, cid in enumerate(cluster_ids)})
        cluster_rows = (
            await session.execute(
                text(
                    f"SELECT chunk_id, heading, text FROM kb_chunks "  # noqa: S608
                    f"WHERE workspace_id = :w AND chunk_id IN ({placeholders})"
                ),
                params,
            )
        ).all()

    if not cluster_rows:
        return 0

    # LLM writes a section summary for the cluster.
    combined = "\n\n".join(
        f"[{r.heading or 'untitled'}]\n{r.text}" for r in cluster_rows
    )
    heading = seed_heading or "Mapped cluster"

    from packages.core.summarize import _summarise, _SECTION_PROMPT

    body = await _summarise(_SECTION_PROMPT, combined, fallback=combined[:400])

    # Persist the summary.
    covers = [r.chunk_id for r in cluster_rows]
    cid = summary_chunk_id("section_summary", covers)

    async with Session() as session:
        await write_summary(
            session,
            scope,
            chunk_id=cid,
            item_id=seed.get("item_id"),
            kind="section_summary",
            heading=heading,
            body=body,
            covers=covers,
            generated_by="mapper",
            probe_question=question,
        )

        # Record the mapper run for the UI feed.
        await session.execute(
            text(
                "INSERT INTO mapper_runs "
                "(workspace_id, collection_id, question, chunks_mapped) "
                "VALUES (:w, :c, :q, :n)"
            ),
            {
                "w": scope.workspace_id,
                "c": scope.collection_id,
                "q": question[:500],
                "n": len(covers),
            },
        )
        await session.commit()

    log.info(
        "mapper: mapped %d chunks in %s (probe: %s)",
        len(covers),
        scope.collection_id,
        question[:80],
    )
    return len(covers)


__all__ = ["map_unmapped_all", "mapper_enabled", "mapper_interval"]
