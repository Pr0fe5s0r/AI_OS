from __future__ import annotations

import asyncio
import hashlib
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

# How long one queued job may run before arq abandons it.
#
# This was 300s, and 300s is a number that fits an ordinary document and nothing
# else. Measured on a 170-page, 16.5MB PDF: parsing took 240s in its own job,
# then embedding produced 11,460 passages and was killed at 299.98s — the
# document was fully parsed and stored, and then silently had no vectors. The
# upload looked finished and the document was unsearchable.
#
# The work is genuinely long and it is not stuck: embedding is one provider
# round trip per batch of 64, and eleven thousand passages is 180 batches. A
# ceiling has to exist so a wedged job frees its slot, but it has to be set
# against the largest REAL document rather than the median one.
#
# 30 minutes. Individual provider calls are separately bounded by
# LLM_TIMEOUT_SECONDS, so this never becomes "wait forever" — it is the budget
# for many bounded calls, not permission for one unbounded one.
JOB_TIMEOUT_SECONDS = int(os.getenv("JOB_TIMEOUT_SECONDS", "1800"))


def _summaries_enabled() -> bool:
    """Whether a document gets a card and section summaries after it is indexed.

    ON by default, which it was not. It was an optional navigation nicety when
    the only thing it fed was a richer catalogue. It stopped being optional when
    retrieval started ROUTING on it: on a store larger than the catalogue window
    the card is what decides which documents a question is even allowed to see,
    and a store with no cards falls back to ranking on passages alone.

    The cost is real and is one model call per section plus one for the card —
    thirteen or so for a typical circular, not one. It runs in the worker, after
    indexing, and a failure there never touches the document that was uploaded.
    Set SUMMARIES_ENABLED=false to turn it off; retrieval still works, with the
    weaker routing signal, and says so via ``fell_back``.
    """
    return os.getenv("SUMMARIES_ENABLED", "true").lower() in ("1", "true", "yes")


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
    data: bytes | str,
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
    if isinstance(data, str):
        if blobs.enabled() and await blobs.exists(data):
            raw_data, _ = await blobs.get(data)
            data = raw_data
        else:
            raise UnsupportedFormat(f"Staging file reference expired or missing: {data}")

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
    except Exception as exc:  # noqa: BLE001 - see below; nothing may vanish here
        # A parser can fail in ways it never anticipated: a truncated upload, a
        # corrupt archive (zlib.error), malformed XML inside a valid zip. Those
        # raised straight through this function, killed the job, and left NO
        # ROW AT ALL — the file simply disappeared, and the console showed it
        # stuck on "indexing…" forever with nothing to explain it. Found by
        # uploading a damaged .docx through the UI.
        #
        # An unreadable file must land as a visible, re-runnable failure like
        # any other. The exception type is kept in the reason because "corrupt
        # or truncated" is a guess, and a caller reporting a bug needs to know
        # what actually broke.
        log.exception("could not parse %s", filename)
        # QUALIFIED, because several standard-library exception classes are
        # named just "error": zlib.error, csv.error, struct.error. A reason
        # reading "could not be read (error)" tells a bug report nothing, which
        # was the first version of this line.
        kind = f"{type(exc).__module__}.{type(exc).__name__}".removeprefix("builtins.")
        reason = (
            f"{filename} could not be read ({kind}). The file may be corrupt or "
            "incompletely uploaded — try uploading it again, or re-export it "
            "from the application that produced it."
        )
        async with Session() as session:
            item_id = await record_failure(session, scope, ref, filename, reason)
            await session.commit()
        return {"item_id": item_id, "outcome": "failed", "reason": reason}

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

    texts = [chunk_module.embedding_text(p.heading, p.text, item.title) for p in passages]
    hashes = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]

    # What this document was embedded as last time. A re-ingest usually changes
    # a handful of passages and leaves the rest untouched, and re-embedding the
    # untouched ones is the single largest avoidable cost in the system:
    # embedding is ~98% of ingest wall-clock, and on a 182-passage spec a
    # one-clause edit was paying for all 182 again.
    #
    # Reuse is also MORE consistent than re-embedding, not less. The provider
    # is not deterministic — the same text embedded twice comes back with
    # cosine 0.9999, not 1.0 — so keeping the vector a passage already has is
    # what stops unchanged text drifting in the index for no reason.
    reusable = await graph.vectors_by_text_hash(scope, item.id)
    missing = [i for i, h in enumerate(hashes) if h not in reusable]

    fresh: list[list[float]] = []
    if missing:
        # One call for the lot. Passage-level embedding multiplies the number
        # of vectors per document by an order of magnitude, so doing them one
        # at a time would turn a 28-passage document into 28 round trips.
        fresh = await asyncio.to_thread(embed_many, [texts[i] for i in missing])

    vectors: list[list[float]] = [reusable.get(h, []) for h in hashes]
    for slot, vector in zip(missing, fresh, strict=True):
        vectors[slot] = vector

    written = await graph.replace_chunks(
        scope,
        item.id,
        [
            {
                "chunk_id": p.chunk_id,
                "ordinal": p.ordinal,
                "heading": p.heading,
                "title": item.title,
                "text_hash": text_hash,
                "embedding": vector,
            }
            for p, text_hash, vector in zip(passages, hashes, vectors, strict=True)
        ],
    )
    # Index summaries: a navigable semantic layer over the passages. Enqueued
    # AFTER embedding because summaries need the chunk nodes in Neo4j in order
    # to attach SUMMARIZES edges. Gated separately from consolidation — this
    # adds navigation but never archives or rewrites passages.
    if _summaries_enabled() and (redis := ctx.get("redis")) is not None:
        await redis.enqueue_job(
            "summarize_item", workspace_id, collection_id, item.id
        )

    # embedded/reused are what the vector-reuse work is measured by: a re-index
    # that changed one clause should report almost every passage reused, and
    # without the counts here that claim cannot be checked from outside.
    return {
        "item_id": item.id,
        "outcome": "embedded",
        "chunks": written,
        "embedded": len(missing),
        "reused": len(passages) - len(missing),
    }


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


async def summarize_item(
    ctx: dict[str, Any],
    workspace_id: str,
    collection_id: str | None,
    item_id: str,
    force: bool = False,
) -> dict[str, Any]:
    """Generate index summaries for one document — a card + section summaries.

    A SEPARATE background job, always — never inline in a request or in the
    ingest path. It is enqueued only AFTER embed_item, so the chunk nodes it
    hangs SUMMARIZES edges off already exist. Every model call inside runs on a
    worker thread (see summarize.py), so a slow provider cannot block the loop.

    Isolated on purpose: a failure here is logged and returned, never raised —
    summarising is a best-effort navigation aid, and nothing it does may stall
    or crash ingestion, the API, or the worker. Gated by SUMMARIES_ENABLED
    unless ``force`` (the explicit "generate now" endpoint).
    """
    if not force and not _summaries_enabled():
        return {"item_id": item_id, "outcome": "disabled"}

    from packages.core.summarize import summarize_document

    scope = _scope(workspace_id, collection_id)
    try:
        async with Session() as session:
            item = await get_item(session, scope, item_id)
            if item is None:
                return {"item_id": item_id, "outcome": "missing"}
            written = await summarize_document(
                session, scope, item.id, item.title, generated_by="ingest"
            )
    except Exception as exc:  # noqa: BLE001 - a summary failure must never escalate
        log.exception("summarize_item failed for %s", item_id)
        return {"item_id": item_id, "outcome": "failed", "error": str(exc)[:200]}
    return {"item_id": item_id, "outcome": "summarized", "summaries": written}


class WorkerSettings:
    """arq worker entry point."""

    functions = [ingest_file, ingest_text, embed_item, classify_new_item, summarize_item]
    redis_settings = redis_settings()
    max_tries = 3
    job_timeout = JOB_TIMEOUT_SECONDS

    @staticmethod
    async def on_startup(ctx: dict[str, Any]) -> None:
        await graph.bootstrap()
        await blobs.ensure_bucket()

    @staticmethod
    async def on_shutdown(ctx: dict[str, Any]) -> None:
        await graph.close_driver()
