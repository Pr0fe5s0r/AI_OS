from __future__ import annotations

import asyncio
import os
import random
from datetime import datetime

from arq.connections import RedisSettings

from packages.connectors.base import field_presence, validate_raw
from packages.core import connector_health as ch
from packages.core import graph
from packages.core.db import Session
from packages.core.ingest import ingest
from packages.core.llm import embed
from packages.core.resolve import resolve
from packages.core.store import get_event, store_event

# Generic arq ingestion pipeline. Jobs take everything as DATA (source_config
# and the profile's things/links slots), so the core never needs to know which
# company or source produced them.
#
# Flow:  ingest_raw -> (commit to Postgres) -> embed_event (Neo4j mirror +
# vector) -> resolve_event (Things + typed links, all in Cypher via core.graph)
# Resilient: the event is committed before embedding; if embedding fails, only
# the (retryable) embed job fails — the event has already landed.
#
# Self-monitoring (checkpoint 6, part A): ingest_raw validates the raw payload
# against its connector's declared schema BEFORE normalizing. A validation
# failure records to connector_health and returns — it never raises, so it
# never crashes ingestion for any other (already-independently-queued) event.

_FIELD_SAMPLE_RATE = 0.01  # ~1% of ingested events get a field-completeness check


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://localhost:6379/0"))


async def ingest_raw(
    ctx, source_config: dict, raw: dict, resolve_cfg: dict | None = None, backfilled: bool = False
) -> str:
    """Normalize a raw payload -> Event, append to the store, queue embedding.

    ``backfilled`` distinguishes a history walk (checkpoint 2, part A) from a
    live sync/webhook — the watcher engine only evaluates backfilled=false
    events, so old history can enrich norms without triggering fresh alerts.
    """
    company_id = source_config.get("company_id", "default")
    connector_type = source_config.get("connector_type")

    if connector_type:
        error = validate_raw(connector_type, raw)
        async with Session() as session:
            if error:
                await ch.record_failure(session, connector_type, company_id, error)
                await session.commit()
                return ""
            await ch.record_success(session, connector_type, company_id)
            if random.random() < _FIELD_SAMPLE_RATE:
                await ch.record_sample(session, connector_type, company_id, field_presence(connector_type, raw))
            await session.commit()

    event = ingest(source_config, raw, backfilled=backfilled)
    async with Session() as session:
        await store_event(session, event)
        await session.commit()
    await ctx["redis"].enqueue_job(
        "embed_event",
        event.id,
        event.company_id,
        event.content,
        event.timestamp.isoformat(),
        event.source,
        resolve_cfg,
    )
    return event.id


async def backfill_source(
    ctx, spec: dict, resolve_cfg: dict | None = None, since_days: int = 90
) -> dict:
    """Walk one connector's history back to ``since_days`` ago, tagging every
    event backfilled=true (checkpoint 2, part A).

    Low priority by nature, not by a dedicated queue: arq has no separate
    priority lanes, so "low priority" here means it paces itself (each
    connector's own polite rate limiting between pages) and processes raws
    one at a time in this single job rather than fanning out a burst of
    concurrent enqueues the way a live sync's fan-out does — it never
    contends for a source's rate-limit budget against a live sync.
    """
    from packages.connectors.base import build_connector

    connector = build_connector(spec)
    backfill = getattr(connector, "backfill", None)
    raws = await backfill(since_days) if backfill is not None else await connector.fetch_raw()

    stored = 0
    for raw in raws:
        event_id = await ingest_raw(ctx, spec["source_config"], raw, resolve_cfg, backfilled=True)
        if event_id:
            stored += 1
    return {"source": spec["source"], "since_days": since_days, "fetched": len(raws), "stored": stored}


async def embed_event(
    ctx,
    event_id: str,
    company_id: str,
    content: str,
    event_time: str,
    source: str,
    resolve_cfg: dict | None = None,
) -> str:
    """Embed one event and mirror it into the graph. Raising lets arq retry."""
    vector = await asyncio.to_thread(embed, content)
    await graph.mirror_event(
        company_id=company_id,
        event_id=event_id,
        event_time=datetime.fromisoformat(event_time),
        source=source,
        embedding=vector,
    )
    if resolve_cfg:
        await ctx["redis"].enqueue_job("resolve_event", event_id, company_id, resolve_cfg)
    return event_id


async def resolve_event(ctx, event_id: str, company_id: str, resolve_cfg: dict) -> int:
    """Link this event into the knowledge graph (incremental Understand layer).

    resolve_cfg = {"things": <profile.things>, "links": <profile.links>}
    """
    async with Session() as session:
        event = await get_event(session, company_id, event_id)
        if event is None:
            return 0
        links = await resolve(
            session, event, resolve_cfg.get("things", {}), resolve_cfg.get("links", {})
        )
        await session.commit()
    return len(links)
