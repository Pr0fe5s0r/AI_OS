from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any

from arq.connections import RedisSettings

from packages.core import graph
from packages.core.db import Session
from packages.core.llm import embed
from packages.core.normalise import Normalised, UnsupportedFormat, normalise, normalise_text
from packages.core.store import get_item, mark_failed, put_item, stable_item_id
from packages.shared.schema import Item, Scope, SourceRef

# ---------------------------------------------------------------------------
# The ingestion pipeline. Everything the KB is given enters through here, and
# nothing user-facing ever waits on it (KB-3).
#
#   ingest_*  ->  normalise to Markdown
#             ->  write to the item index (the hash decides create/skip/version)
#             ->  embed ONLY if the content actually changed
#
# The embed step is a separate job on purpose: the item is committed first, so
# a failing embedding costs a retry of the embedding, not the ingestion. And
# because put_item reports `unchanged`, re-syncing a drive that has not moved
# performs no model calls at all — the cost control in KB-8 is that check,
# not a policy engine layered on top of it.
# ---------------------------------------------------------------------------


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://localhost:6379/0"))


def _scope(workspace_id: str, collection_id: str | None) -> Scope:
    return Scope(workspace_id=workspace_id, collection_id=collection_id)


async def _store(
    ctx: dict[str, Any],
    scope: Scope,
    source: SourceRef,
    parsed: Normalised,
    period_start: datetime | None,
    period_end: datetime | None,
    metadata: dict[str, Any] | None,
    suggested: str | None = None,
) -> dict[str, Any]:
    """Shared tail: index the normalised content, queue embedding if it moved."""
    item = Item(
        id=stable_item_id(scope, source),
        scope=scope,
        title=parsed.title,
        body=parsed.body,
        source=source,
        period_start=period_start,
        period_end=period_end,
        metadata={**parsed.metadata, **(metadata or {})},
    )

    async with Session() as session:
        result = await put_item(session, item)
        await session.commit()

    if result.embedded_needed and (redis := ctx.get("redis")) is not None:
        await redis.enqueue_job("embed_item", scope.workspace_id, scope.collection_id, result.item.id)
        await redis.enqueue_job(
            "classify_new_item", scope.workspace_id, scope.collection_id, result.item.id, suggested
        )

    return {
        "item_id": result.item.id,
        "version": result.item.version,
        "outcome": result.outcome,
    }


async def ingest_file(
    ctx: dict[str, Any],
    workspace_id: str,
    collection_id: str | None,
    source: str,
    locator: str,
    filename: str,
    data: bytes,
    url: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    suggested: str | None = None,
) -> dict[str, Any]:
    """A file — uploaded or pulled from a source — becomes an item.

    The bytes are read, converted, and dropped. What survives is Markdown plus
    the link back (KB-7): the KB is not a file store.
    """
    scope = _scope(workspace_id, collection_id)
    ref = SourceRef(source=source, locator=locator, url=url, fetched_at=datetime.now())
    try:
        parsed = await asyncio.to_thread(normalise, data, filename)
    except UnsupportedFormat as exc:
        # Visible and re-runnable, never silent. The item id is deterministic,
        # so a later retry lands on the same row rather than orphaning this one.
        item_id = stable_item_id(scope, ref)
        async with Session() as session:
            existing = await get_item(session, scope, item_id)
            if existing is not None:
                await mark_failed(session, scope, item_id, existing.version, str(exc))
                await session.commit()
        return {"item_id": item_id, "outcome": "failed", "reason": str(exc)}

    return await _store(ctx, scope, ref, parsed, period_start, period_end, metadata, suggested)


async def ingest_text(
    ctx: dict[str, Any],
    workspace_id: str,
    collection_id: str | None,
    source: str,
    locator: str,
    body: str,
    title: str | None = None,
    url: str | None = None,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    suggested: str | None = None,
) -> dict[str, Any]:
    """Content that already is text: a generated report, a distilled session."""
    scope = _scope(workspace_id, collection_id)
    ref = SourceRef(source=source, locator=locator, url=url, fetched_at=datetime.now())
    parsed = normalise_text(body, title=title, source_name=locator)
    return await _store(ctx, scope, ref, parsed, period_start, period_end, metadata, suggested)


async def embed_item(
    ctx: dict[str, Any], workspace_id: str, collection_id: str | None, item_id: str
) -> dict[str, Any]:
    """Mirror the item into the graph and give it a vector.

    Runs only for content that changed. Retryable in isolation: the item is
    already committed, so a failure here costs recall until the retry, never
    the item itself.
    """
    scope = _scope(workspace_id, collection_id)
    async with Session() as session:
        item = await get_item(session, scope, item_id)
    if item is None:
        return {"item_id": item_id, "outcome": "missing"}

    vector = await asyncio.to_thread(embed, f"{item.title}\n\n{item.body}")
    await graph.upsert_item(
        scope,
        item_id=item.id,
        title=item.title,
        source=item.source.source,
        status=str(item.status),
        embedding=vector,
    )
    return {"item_id": item.id, "outcome": "embedded"}


async def classify_new_item(
    ctx: dict[str, Any],
    workspace_id: str,
    collection_id: str | None,
    item_id: str,
    suggested: str | None = None,
) -> dict[str, Any]:
    """File the item. Separate from ingestion so a slow or unavailable
    classifier delays filing, never the write."""
    from packages.core.classify import classify_item

    scope = _scope(workspace_id, collection_id)
    async with Session() as session:
        item = await get_item(session, scope, item_id)
        if item is None:
            return {"item_id": item_id, "outcome": "missing"}
        assignments = await classify_item(session, scope, item, suggested)
        await session.commit()
    return {
        "item_id": item_id,
        "classes": [a.class_id for a in assignments],
        "needs_review": any(a.needs_review for a in assignments),
    }


class WorkerSettings:
    """arq worker entry point."""

    functions = [ingest_file, ingest_text, embed_item, classify_new_item]
    redis_settings = redis_settings()
    max_tries = 3
    job_timeout = 300

    @staticmethod
    async def on_startup(ctx: dict[str, Any]) -> None:
        await graph.bootstrap()

    @staticmethod
    async def on_shutdown(ctx: dict[str, Any]) -> None:
        await graph.close_driver()
