from __future__ import annotations

import asyncio

from arq import create_pool
from sqlalchemy.ext.asyncio import AsyncSession

from packages.connectors.base import build_connector
from packages.core.db import Session
from packages.core.pipeline import redis_settings
from verticals.software.config import COMPANY_ID, ENTITY_RULES, connector_config

# The vertical trigger: pull raw payloads from each connected source and enqueue
# them onto the generic arq pipeline. The worker normalizes -> stores -> embeds
# -> resolves into the graph. No mock data.


async def trigger_ingest(
    session: AsyncSession,
    company_id: str = COMPANY_ID,
    only_source: str | None = None,
    wait: float | None = None,
) -> dict:
    """Fetch from every connected source and enqueue the raw payloads.

    `wait` blocks until each enqueued event has actually been committed. The
    scheduled scan needs this: detection reads events straight from the store, so
    analysing before the queue drains would silently miss the issue just opened.
    Interactive callers pass nothing and return as soon as the work is queued.
    """
    specs = await connector_config(session, company_id)
    if only_source:
        specs = [s for s in specs if s["type"] == only_source]
    if not specs:
        return {}

    pool = await create_pool(redis_settings())
    counts: dict[str, int] = {}
    jobs = []
    try:
        for spec in specs:
            connector = build_connector(spec)
            raws = await connector.fetch_raw()
            for raw in raws:
                job = await pool.enqueue_job("ingest_raw", spec["source_config"], raw, ENTITY_RULES)
                if job is not None:
                    jobs.append(job)
            counts[spec["type"]] = len(raws)
        if wait and jobs:
            # a slow or failed ingest must not abort the scan — analyse whatever landed
            await asyncio.gather(
                *(job.result(timeout=wait) for job in jobs), return_exceptions=True
            )
    finally:
        await pool.aclose()
    return counts


async def _main() -> None:
    async with Session() as session:
        result = await trigger_ingest(session)
    if not result:
        print("no connectors configured. Connect a source in the UI (or set GITHUB_REPO).")
    else:
        print("enqueued:", ", ".join(f"{k}={v}" for k, v in result.items()))


if __name__ == "__main__":
    asyncio.run(_main())
