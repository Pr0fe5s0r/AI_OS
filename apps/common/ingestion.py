from __future__ import annotations

import asyncio

from arq import create_pool
from sqlalchemy.ext.asyncio import AsyncSession

from apps.common.context import (
    build_source_config,
    connector_specs,
    push_source,
    resolve_cfg,
)
from packages.connectors.base import build_connector
from packages.core.pipeline import redis_settings
from packages.core.profile import Profile

# Ingestion triggers: pull (connectors) and push (generic HTTP feed). Both
# enqueue raw payloads onto the same core pipeline: normalize -> store ->
# embed (Neo4j mirror) -> resolve (Things + typed links).


async def trigger_ingest(
    session: AsyncSession,
    profile: Profile,
    only_source: str | None = None,
    wait: float | None = None,
) -> dict:
    """Fetch from every connected source and enqueue the raw payloads.

    `wait` blocks until each enqueued event has been committed — the scheduled
    scan needs that, because detection reads events straight from the store.
    """
    specs = await connector_specs(session, profile)
    if only_source:
        specs = [s for s in specs if s["source"] == only_source]
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
                job = await pool.enqueue_job(
                    "ingest_raw", spec["source_config"], raw, resolve_cfg(profile)
                )
                if job is not None:
                    jobs.append(job)
            counts[spec["source"]] = len(raws)
        if wait and jobs:
            # a slow or failed ingest must not abort the scan — analyse whatever landed
            await asyncio.gather(
                *(job.result(timeout=wait) for job in jobs), return_exceptions=True
            )
    finally:
        await pool.aclose()
    return counts


async def trigger_backfill(
    session: AsyncSession, profile: Profile, only_source: str | None = None, since_days: int = 90
) -> dict:
    """Queue one low-priority backfill job per connected source (checkpoint 2,
    part A). Each job walks its own connector's history and paces itself —
    see packages.core.pipeline.backfill_source."""
    specs = await connector_specs(session, profile)
    if only_source:
        specs = [s for s in specs if s["source"] == only_source]
    if not specs:
        return {}

    pool = await create_pool(redis_settings())
    queued: dict[str, str] = {}
    try:
        for spec in specs:
            await pool.enqueue_job("backfill_source", spec, resolve_cfg(profile), since_days)
            queued[spec["source"]] = "queued"
    finally:
        await pool.aclose()
    return queued


async def push_events(
    profile: Profile, source: str, raws: list[dict], wait: float | None = None
) -> int:
    """Enqueue pushed raw payloads for a `kind: push` profile source."""
    source_def = push_source(profile, source)
    if source_def is None:
        raise ValueError(f"profile {profile.company_id!r} has no push source {source!r}")

    source_config = build_source_config(profile, source_def, {})
    pool = await create_pool(redis_settings())
    jobs = []
    try:
        for raw in raws:
            job = await pool.enqueue_job("ingest_raw", source_config, raw, resolve_cfg(profile))
            if job is not None:
                jobs.append(job)
        if wait and jobs:
            await asyncio.gather(
                *(job.result(timeout=wait) for job in jobs), return_exceptions=True
            )
    finally:
        await pool.aclose()
    return len(raws)
