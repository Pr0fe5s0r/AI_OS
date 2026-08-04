from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from datetime import datetime
from typing import Any

from arq.connections import RedisSettings

from packages.core import blobs, chunks, graph, pages
from packages.core import chunk as chunk_module
from packages.core.db import Session
from packages.core.llm import embed_many
from packages.core.normalise import (
    Normalised,
    ScannedDocument,
    UnsupportedFormat,
    normalise,
    normalise_text,
    title_from,
)
from packages.core.store import get_item, put_item, record_failure, stable_item_id
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


log = logging.getLogger(__name__)


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://localhost:6379/0"))


async def _keep_original(
    scope: Scope, ref: SourceRef, filename: str, data: bytes
) -> dict[str, Any] | None:
    """Store the uploaded bytes so the file can be shown as it arrived.

    Best-effort: a failure to keep the original must never fail the ingest — the
    document is still indexed and retrievable, it just cannot be previewed as a
    PDF/image. Returns the metadata to record on the item, or None when nothing
    was kept (no store configured, or the file is over the size cap).
    """
    if not blobs.enabled() or len(data) > blobs.max_original_bytes():
        return None
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    item_id = stable_item_id(scope, ref)
    try:
        await blobs.put(blobs.key_for(scope.workspace_id, item_id), data, content_type)
    except Exception:  # noqa: BLE001 - keeping the original is best-effort
        log.exception("could not store original for %s", filename)
        return None
    return {"filename": filename, "content_type": content_type, "size": len(data)}


async def _read_scan(
    data: bytes, filename: str, refusal: ScannedDocument
) -> Normalised | None:
    """A scanned PDF, read page by page with vision. None if it cannot be.

    The result is deliberately shaped exactly like extracted PDF text — the
    same `<!-- page N -->` markers, the same rules between pages — so nothing
    downstream needs to know or care how the words were obtained. The page
    index builds its sections the same way, passages chunk the same way,
    citations resolve the same way.

    None when there is no vision model configured, or when every page came back
    empty. Both fall through to the failure that was recorded before this
    existed: an item with no text looks ingested and can never be retrieved,
    which is worse than a refusal that says why.
    """
    if not pages.available():
        return None

    transcribed, total = await pages.transcribe_document(data)
    written = [
        f"<!-- page {number} -->\n{text}"
        for number, text in enumerate(transcribed, start=1)
        if text.strip()
    ]
    if not written:
        return None

    body = "\n\n---\n\n".join(written)
    if len(transcribed) < total:
        # Said on the page itself, not only in metadata: a reader who searches
        # this document and finds nothing deserves to know the back half was
        # never read, rather than concluding it is not there.
        body += (
            f"\n\n---\n\n<!-- page {len(transcribed) + 1} -->\n"
            f"*Pages {len(transcribed) + 1}–{total} of this scan were not read. "
            f"The reading limit is {pages.max_transcribe_pages()} pages.*"
        )

    log.info("read %d/%d scanned pages of %s with vision", len(written), total, filename)

    # A picture's own description makes a terrible title: the first line of it
    # is a sentence ABOUT the file ("This image is a bar chart."), and a list
    # of documents titled that way tells a reader nothing about which is
    # which. An image's filename is the only name it has, so it is the name it
    # gets. A scanned PDF keeps the usual rule, because its first line is
    # genuinely its heading.
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    picture = pages.is_image("", filename)
    if picture:
        stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        readable = stem.replace("_", " ").replace("-", " ").strip()
        title = (readable[:1].upper() + readable[1:])[:200] or filename
    else:
        title = title_from(body, filename)

    return Normalised(
        title=title,
        body=body,
        metadata={
            "pages": total,
            "format": suffix if picture else "pdf",
            # How this document came to have words at all. It travels with the
            # item because a transcription is a reading of a picture, not the
            # document's own text, and anything built on it should be able to
            # tell the difference.
            "read_by": "vision",
            "pages_read": len(written),
        },
    )


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

    The bytes are converted to Markdown for indexing, and the original is also
    kept in object storage so the file can be shown as it arrived (a PDF as a
    PDF). A file that fails to parse keeps no original either — there is nothing
    a person could usefully preview.
    """
    scope = _scope(workspace_id, collection_id)
    ref = SourceRef(source=source, locator=locator, url=url, fetched_at=datetime.now())
    try:
        parsed = await asyncio.to_thread(normalise, data, filename)
    except ScannedDocument as exc:
        # Pages of pictures with no text layer. Refused outright until now,
        # which meant the documents with the strongest case for being read as
        # images were the only ones that could not get in at all.
        read_by_eye = await _read_scan(data, filename, exc)
        if read_by_eye is None:
            async with Session() as session:
                item_id = await record_failure(session, scope, ref, filename, str(exc))
                await session.commit()
            return {"item_id": item_id, "outcome": "failed", "reason": str(exc)}
        parsed = read_by_eye
    except UnsupportedFormat as exc:
        # Visible and re-runnable, never silent. The item id is deterministic,
        # so a later retry lands on the same row rather than orphaning this one.
        async with Session() as session:
            item_id = await record_failure(session, scope, ref, filename, str(exc))
            await session.commit()
        return {"item_id": item_id, "outcome": "failed", "reason": str(exc)}

    original = await _keep_original(scope, ref, filename, data)
    merged = {**(metadata or {}), "original": original} if original else metadata
    return await _store(ctx, scope, ref, parsed, period_start, period_end, merged, suggested)


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

    # The document node carries identity and lineage; it holds no vector of
    # its own. Embedding a whole document produced the average of everything
    # it said, which matched nothing it said.
    await graph.upsert_item(
        scope,
        item_id=item.id,
        title=item.title,
        source=item.source.source,
        status=str(item.status),
    )

    # The passages already exist — the store wrote them with the document, so
    # keyword search has been working since the write. This job only gives them
    # vectors, which is the part that needs a provider.
    async with Session() as session:
        passages = await chunks.for_item(session, scope, item.id)

    if not passages:
        return {"item_id": item.id, "outcome": "empty", "chunks": 0}

    # One call for the lot. Passage-level embedding multiplies the number of
    # vectors per document by an order of magnitude, so doing them one at a
    # time would turn a 28-passage document into 28 round trips.
    vectors = await asyncio.to_thread(
        embed_many,
        [chunk_module.embedding_text(p.heading, p.text, item.title) for p in passages],
    )

    written = await graph.replace_chunks(
        scope,
        item.id,
        [
            {
                "chunk_id": p.chunk_id,
                "ordinal": p.ordinal,
                "heading": p.heading,
                "title": item.title,
                "embedding": vector,
            }
            for p, vector in zip(passages, vectors, strict=True)
        ],
    )
    return {"item_id": item.id, "outcome": "embedded", "chunks": written}


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
        await blobs.ensure_bucket()

    @staticmethod
    async def on_shutdown(ctx: dict[str, Any]) -> None:
        await graph.close_driver()
