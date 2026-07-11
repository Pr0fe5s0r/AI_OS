from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.norms import compute_baselines
from packages.core.resolve import resolve
from packages.core.store import get_event
from verticals.software.config import COMPANY_ID, ENTITY_RULES, NORM_DEFINITIONS

# Understand layer for the software vertical. Resolution normally runs
# incrementally in the worker (resolve_event job); build_graph() is the
# batch/backfill path for events ingested before the graph existed.


async def build_graph(session: AsyncSession, company_id: str = COMPANY_ID) -> dict:
    rows = await session.execute(
        text("SELECT id FROM events WHERE company_id = :c ORDER BY timestamp ASC"),
        {"c": company_id},
    )
    ids = [r.id for r in rows]

    links = 0
    for event_id in ids:
        event = await get_event(session, company_id, event_id)
        if event is None:
            continue
        links += len(await resolve(session, event, ENTITY_RULES))
    await session.commit()

    counts = (
        await session.execute(
            text(
                """
                SELECT (SELECT count(*) FROM nodes WHERE company_id = :c) AS nodes,
                       (SELECT count(*) FROM edges WHERE company_id = :c) AS edges
                """
            ),
            {"c": company_id},
        )
    ).one()
    return {"events": len(ids), "links": links, "nodes": counts.nodes, "edges": counts.edges}


async def compute_norms(session: AsyncSession, company_id: str = COMPANY_ID) -> list[dict]:
    """Learn baselines for this vertical's metrics (core does the stats)."""
    baselines = await compute_baselines(session, company_id, NORM_DEFINITIONS)
    await session.commit()
    return [json.loads(b.model_dump_json()) for b in baselines]
