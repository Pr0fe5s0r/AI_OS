from __future__ import annotations

import asyncio
import os

from arq.connections import RedisSettings

from packages.core.db import Session
from packages.core.ingest import ingest
from packages.core.llm import embed
from packages.core.resolve import resolve
from packages.core.store import get_event, store_embedding, store_event

# Generic arq ingestion pipeline. Jobs take everything as DATA (source_config,
# entity_rules, raw payload), so the core never needs to know which vertical or
# source produced them.
#
# Flow:  ingest_raw -> (commit) -> embed_event -> resolve_event
# Resilient: the event is committed before embedding; if embedding fails, only
# the (retryable) embed job fails — the event has already landed.


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://localhost:6379/0"))


async def ingest_raw(ctx, source_config: dict, raw: dict, entity_rules: dict | None = None) -> str:
    """Normalize a raw payload -> Event, append to the store, queue embedding."""
    event = ingest(source_config, raw)
    async with Session() as session:
        await store_event(session, event)
        await session.commit()
    await ctx["redis"].enqueue_job(
        "embed_event", event.id, event.company_id, event.content, entity_rules
    )
    return event.id


async def embed_event(
    ctx, event_id: str, company_id: str, content: str, entity_rules: dict | None = None
) -> str:
    """Embed one event. Raising here lets arq retry; the event is already stored."""
    vector = await asyncio.to_thread(embed, content)
    async with Session() as session:
        await store_embedding(session, event_id, vector)
        await session.commit()
    if entity_rules:
        await ctx["redis"].enqueue_job("resolve_event", event_id, company_id, entity_rules)
    return event_id


async def resolve_event(ctx, event_id: str, company_id: str, entity_rules: dict) -> int:
    """Link this event into the knowledge graph (incremental Understand layer)."""
    async with Session() as session:
        event = await get_event(session, company_id, event_id)
        if event is None:
            return 0
        links = await resolve(session, event, entity_rules)
        await session.commit()
    return len(links)
