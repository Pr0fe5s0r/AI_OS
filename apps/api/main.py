from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from arq import create_pool
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.auth_routes import router as auth_router
from apps.common.consolidation import enabled as consolidation_enabled
from apps.common.consolidation import interval_seconds as consolidation_interval
from packages.core import blobs, graph
from packages.core.answer import answer
from packages.core.chunks import by_ids as chunks_by_ids
from packages.core.chunks import for_item as chunks_for_item
from packages.core.classify import (
    bulk_override,
    classes_for,
    create_class,
    delete_class,
    list_classes,
    needs_review,
    override,
)
from packages.core.collections import (
    create_cluster,
    create_collection,
    delete_collection,
    get_collection,
    list_clusters,
    rename_collection,
)
from packages.core.consolidate import recent_runs
from packages.core.consolidate import run_once as run_consolidation
from packages.core.db import Session
from packages.core.graph import chunk_lineage as graph_lineage
from packages.core.keys import create_key, list_keys, revoke_key
from packages.core.neighbours import collection_graph
from packages.core.normalise import can_parse, supported
from packages.core.pipeline import redis_settings
from packages.core.search import RetrievalConfig, search_traced
from packages.core.snippets import build as build_snippets
from packages.core.store import get_item, item_versions, list_items
from packages.core.tenancy import (
    enforce_binding,
    require_write,
    resolve_caller,
    workspace_scope,
)
from packages.core.tracing import get_trace, list_traces, record, stats
from packages.shared.schema import Lifecycle, Scope

# ---------------------------------------------------------------------------
# The Knowledge Base API. Two contracts and nothing else of consequence:
#
#   POST /api/items    the ingest contract    — one write path for everything
#   GET  /api/search   the retrieval contract — one read path for every agent
#
# Both are scoped by the caller's credential — a session cookie or an API key —
# plus an optional verified collection header. No route accepts a workspace id
# as a parameter.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    await graph.bootstrap()
    await blobs.ensure_bucket()
    app.state.queue = await create_pool(redis_settings())
    yield
    await app.state.queue.close()
    await graph.close_driver()


app = FastAPI(title="Knowledge Base", lifespan=lifespan)
# Named origins, not "*": the session travels as a cookie, and a browser
# refuses a wildcard origin on any credentialed request — so "*" would not be
# permissive, it would simply break every call the UI makes.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip()
        for o in os.getenv("WEB_ORIGINS", "http://localhost:3000").split(",")
        if o.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)


async def db() -> Any:
    async with Session() as session:
        yield session


# ------------------------------ ingest contract ------------------------------


class TextIngest(BaseModel):
    """Content that is already text: a report, a distilled session, a note."""

    source: str = Field(min_length=1, description="upload | gdrive | s3 | notion | report | chat")
    locator: str = Field(min_length=1, description="stable id at the source")
    body: str = Field(min_length=1)
    title: str | None = None
    url: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Accepted(BaseModel):
    job_id: str | None
    status: str = "queued"


@app.post("/api/items", response_model=Accepted, status_code=202)
async def ingest_text_item(payload: TextIngest, scope: Scope = Depends(workspace_scope)) -> Accepted:
    """Write text into the KB. Returns immediately — indexing never blocks."""
    job = await app.state.queue.enqueue_job(
        "ingest_text",
        scope.workspace_id,
        scope.collection_id,
        payload.source,
        payload.locator,
        payload.body,
        payload.title,
        payload.url,
        payload.period_start,
        payload.period_end,
        payload.metadata,
    )
    return Accepted(job_id=job.job_id if job else None)


@app.post("/api/items/file", response_model=Accepted, status_code=202)
async def ingest_file_item(
    file: UploadFile = File(...),
    source: str = Form("upload"),
    locator: str | None = Form(None),
    url: str | None = Form(None),
    period_start: datetime | None = Form(None),
    period_end: datetime | None = Form(None),
    scope: Scope = Depends(workspace_scope),
) -> Accepted:
    """Upload a document. The bytes become Markdown and are then discarded.

    Locator defaults to the filename, so re-uploading the same document
    updates it rather than creating a second copy of it.
    """
    filename = file.filename or "upload"
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")

    # Refuse here rather than in the worker. Accepting a file we have no parser
    # for produced the worst of both: the upload reported success, indexing
    # failed in a job whose reason nobody reads, and the console could only say
    # the document was "not searchable yet" — which sounds like a delay.
    if not can_parse(filename):
        raise HTTPException(
            415, f"Cannot read {filename}. Supported formats: {', '.join(supported())}"
        )

    job = await app.state.queue.enqueue_job(
        "ingest_file",
        scope.workspace_id,
        scope.collection_id,
        source,
        locator or filename,
        filename,
        data,
        url,
        period_start,
        period_end,
        None,
    )
    return Accepted(job_id=job.job_id if job else None)


@app.get("/api/formats")
async def formats() -> dict[str, list[str]]:
    """Which file types can be ingested today."""
    return {"supported": list(supported())}


# ----------------------------- retrieval contract -----------------------------


@app.get("/api/search")
async def retrieve(
    q: str = Query(min_length=1),
    limit: int = Query(10, ge=1, le=100),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    sources: list[str] | None = Query(None),
    item_ids: list[str] | None = Query(
        None, description="Restrict the search to these document ids."
    ),
    period_from: datetime | None = None,
    period_to: datetime | None = None,
    include_superseded: bool = False,
    scope: Scope = Depends(workspace_scope),
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """The single read path. Every agent uses this; behaviour comes from config.

    Results carry provenance — source, locator and link — which is what a
    citation is rendered from. Pass `item_ids` to confine the search to specific
    documents; omit it to search the whole (collection-scoped) store.
    """
    cfg = RetrievalConfig(
        limit=limit,
        min_score=min_score,
        sources=tuple(sources or ()),
        item_ids=tuple(item_ids or ()),
        period_from=period_from,
        period_to=period_to,
        include_superseded=include_superseded,
    )
    hits, trace = await search_traced(session, scope, q, cfg)
    await record(
        session,
        scope,
        trace,
        via=str(principal.get("via", "session")),
        actor=str(principal.get("email") or ""),
    )
    await session.commit()

    # Classes ride along so a result can be shown filed, without a call per hit.
    tagged = await classes_for(session, scope, [h.item_id for h in hits])
    return {
        "query": q,
        "count": len(hits),
        # The trace id comes back with the answer, so any result can be taken
        # straight to the explanation of why it was returned.
        "trace_id": trace.trace_id,
        "degraded": trace.degraded,
        "took_ms": trace.duration_ms,
        "results": [
            {**h.model_dump(), "classes": tagged.get(h.item_id, [])} for h in hits
        ],
    }


@app.get("/api/answer")
async def answer_question(
    q: str = Query(min_length=1),
    limit: int = Query(8, ge=1, le=20),
    mode: str = Query("vectorless", pattern="^(hybrid|vectorless)$"),
    sources: list[str] | None = Query(None),
    scope: Scope = Depends(workspace_scope),
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Retrieval, then a written answer built only from what was retrieved.

    Separate from /api/search on purpose. Search is the contract other software
    builds on and returns only what the store holds; this adds a written layer
    on top of it. A caller that must never see generated prose keeps using
    search and is unaffected by anything here.

    The answer carries `grounded`, which is false when the store had nothing to
    answer from, when the model said the passages did not cover the question,
    or when it produced prose citing nothing. That flag is the difference
    between an answer and a guess, so it travels with the text rather than
    being left for the reader to infer.

    `mode` picks how the evidence is found:

      vectorless  reason over each document's table of contents and open the
                  sections that look like they answer it (default)
      hybrid      passage embeddings and keyword matching, fused

    Neither is a strict improvement on the other, so this is a choice rather
    than a migration. The mode comes back on the response, because two answers
    to one question can differ entirely on it.
    """
    cfg = RetrievalConfig(limit=limit, sources=tuple(sources or ()))
    result, trace = await answer(session, scope, q, cfg, mode=mode)

    # Recorded once, under the retrieval that produced it: an answer whose
    # retrieval cannot be inspected is not one anybody can argue with.
    await record(
        session,
        scope,
        trace,
        via=str(principal.get("via", "session")),
        actor=str(principal.get("email") or ""),
    )
    await session.commit()

    tagged = await classes_for(session, scope, [h.item_id for h in result.hits])
    return {
        **result.as_dict(),
        "results": [
            {**h.model_dump(), "classes": tagged.get(h.item_id, [])} for h in result.hits
        ],
    }


# --------------------------------- traces ---------------------------------


@app.get("/api/traces")
async def traces(
    limit: int = Query(50, ge=1, le=200),
    only_empty: bool = False,
    only_degraded: bool = False,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Recent queries. The interesting ones returned nothing or ran degraded."""
    return {
        "traces": await list_traces(
            session, scope, limit=limit, only_empty=only_empty, only_degraded=only_degraded
        )
    }


@app.get("/api/traces/stats")
async def trace_stats(
    hours: int = Query(24, ge=1, le=720),
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """How retrieval has been behaving over a window."""
    return await stats(session, scope, hours=hours)


@app.get("/api/traces/{trace_id}")
async def trace_detail(
    trace_id: str,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """One query, in full: every candidate, every score, every timing."""
    found = await get_trace(session, scope, trace_id)
    if found is None:
        raise HTTPException(404, "No such trace.")
    return found


# -------------------------------- the index --------------------------------


@app.get("/api/items")
async def catalogue(
    source: str | None = None,
    status: Lifecycle = Lifecycle.ACTIVE,
    class_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """What the KB holds — the listing and filtering KB-1 requires."""
    items = await list_items(
        session,
        scope,
        source=source,
        status=status,
        class_id=class_id,
        limit=limit,
        offset=offset,
    )
    tagged = await classes_for(session, scope, [i.id for i in items])
    return {
        "count": len(items),
        "items": [
            {**i.model_dump(), "classes": tagged.get(i.id, [])} for i in items
        ],
    }


@app.get("/api/facets")
async def facets(
    scope: Scope = Depends(workspace_scope), session: AsyncSession = Depends(db)
) -> dict[str, Any]:
    """Counts for the filter rail: how many items per source and per class."""
    by_source = (
        await session.execute(
            text(
                "SELECT source, count(*) AS n FROM kb_items "
                "WHERE workspace_id = :t AND status = 'active' GROUP BY source ORDER BY n DESC"
            ),
            {"t": scope.workspace_id},
        )
    ).all()
    by_class = (
        await session.execute(
            text(
                """
                SELECT ic.class_id, COALESCE(c.name, ic.class_id) AS name, count(*) AS n
                FROM kb_item_classes ic
                JOIN kb_items i ON i.item_id = ic.item_id AND i.status = 'active'
                LEFT JOIN kb_classes c ON c.class_id = ic.class_id
                     AND (c.workspace_id IS NULL OR c.workspace_id = ic.workspace_id)
                WHERE ic.workspace_id = :t
                GROUP BY ic.class_id, c.name ORDER BY n DESC
                """
            ),
            {"t": scope.workspace_id},
        )
    ).all()
    total = (
        await session.execute(
            text(
                "SELECT count(*) FROM kb_items WHERE workspace_id = :t AND status = 'active'"
            ),
            {"t": scope.workspace_id},
        )
    ).scalar_one()
    return {
        "total": total,
        "sources": [{"source": r.source, "count": r.n} for r in by_source],
        "classes": [
            {"class_id": r.class_id, "name": r.name, "count": r.n} for r in by_class
        ],
    }


# ----------------------------- taxonomy (KB-2) -----------------------------


class ClassIn(BaseModel):
    class_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1)
    parent_id: str | None = None
    description: str | None = None


class OverrideIn(BaseModel):
    """A person's filing decision. Sticky, and never silently reverted."""

    class_ids: list[str]
    item_ids: list[str] | None = None  # present = bulk


@app.get("/api/classes")
async def taxonomy(
    scope: Scope = Depends(workspace_scope), session: AsyncSession = Depends(db)
) -> dict[str, Any]:
    """The taxonomy this workspace can file into: platform classes plus their own."""
    return {"classes": await list_classes(session, scope)}


@app.post("/api/classes", status_code=201)
async def add_class(
    payload: ClassIn,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Adding a class is data, not a deployment (KB-2.0)."""
    created = await create_class(
        session,
        scope,
        payload.class_id,
        payload.name,
        parent_id=payload.parent_id,
        description=payload.description,
    )
    await session.commit()
    return created


@app.delete("/api/classes/{class_id}")
async def remove_class(
    class_id: str,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, bool]:
    """Remove one of this workspace's own classes. System classes may be extended
    but not deleted, so this refuses rather than pretending to succeed."""
    removed = await delete_class(session, scope, class_id)
    if not removed:
        raise HTTPException(400, "Not a deletable class for this workspace.")
    await session.commit()
    return {"deleted": True}


@app.put("/api/items/{item_id}/classes")
async def set_classes(
    item_id: str,
    payload: OverrideIn,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
    principal: dict[str, Any] = Depends(resolve_caller),
) -> dict[str, Any]:
    """A person files this item. Audited, and pinned so nothing reverts it."""
    actor = str(principal.get("email") or principal.get("user_id") or "unknown")
    if payload.item_ids:
        count = await bulk_override(session, scope, payload.item_ids, payload.class_ids, actor)
        await session.commit()
        return {"updated": count, "class_ids": payload.class_ids}

    await override(session, scope, item_id, payload.class_ids, actor)
    await session.commit()
    return {"item_id": item_id, "class_ids": payload.class_ids, "pinned": True}


@app.get("/api/review")
async def review_queue(
    limit: int = Query(50, ge=1, le=200),
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """What the KB could not file confidently — surfaced, never buried."""
    return {"items": await needs_review(session, scope, limit=limit)}


@app.get("/api/items/{item_id}")
async def one_item(
    item_id: str,
    version: int | None = None,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """One item: what it is, where it came from, and whether it is current."""
    item = await get_item(session, scope, item_id, version=version)
    if item is None:
        raise HTTPException(404, "No such item.")
    # Filing travels with the item everywhere it is returned. Without this the
    # detail view showed "not filed yet" for an item the list had just shown
    # three categories against.
    tagged = await classes_for(session, scope, [item.id])
    return {**item.model_dump(), "classes": tagged.get(item.id, [])}


@app.get("/api/items/{item_id}/versions")
async def versions(
    item_id: str,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """The lineage of an item, newest first."""
    history = await item_versions(session, scope, item_id)
    if not history:
        raise HTTPException(404, "No such item.")
    return {"item_id": item_id, "versions": [i.model_dump() for i in history]}


@app.get("/api/items/{item_id}/related")
async def related_items(
    item_id: str, hops: int = Query(1, ge=1, le=3), scope: Scope = Depends(workspace_scope)
) -> dict[str, Any]:
    """What this item connects to — lineage and derivation."""
    return (await graph.related(scope, item_id, hops=hops)).model_dump()


@app.get("/api/items/{item_id}/chunks")
async def item_chunks(
    item_id: str,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """The passages a document was split into — what actually got indexed.

    A document is the unit of identity; a chunk is the unit of retrieval. This
    is how a person sees the difference: one uploaded file becomes N passages,
    and this is those N, in document order.
    """
    chunks = await chunks_for_item(session, scope, item_id)
    return {
        "item_id": item_id,
        "count": len(chunks),
        "chunks": [
            {
                "chunk_id": c.chunk_id,
                "ordinal": c.ordinal,
                "heading": c.heading,
                "text": c.text,
            }
            for c in chunks
        ],
    }


# Types a browser can render in place; everything else is served to download.
_INLINE_PREFIXES = ("application/pdf", "image/", "text/")


@app.get("/api/items/{item_id}/original")
async def item_original(
    item_id: str,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> Response:
    """The original file as it was uploaded — a PDF as a PDF, an image as an
    image. 404 when none was kept: text pasted directly, files ingested before
    originals were stored, or anything over the size cap."""
    item = await get_item(session, scope, item_id)
    if item is None:
        raise HTTPException(404, "No such item.")
    original = (item.metadata or {}).get("original")
    if not original:
        raise HTTPException(404, "No original file is stored for this document.")

    try:
        data, content_type = await blobs.get(blobs.key_for(scope.workspace_id, item_id))
    except Exception as exc:  # noqa: BLE001 - any store error is a missing original
        raise HTTPException(404, "The original file is no longer available.") from exc

    media_type = content_type or original.get("content_type") or "application/octet-stream"
    disposition = "inline" if media_type.startswith(_INLINE_PREFIXES) else "attachment"
    filename = str(original.get("filename") or item_id).replace('"', "")
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


# ---------------------------------- brands ----------------------------------


class BrandIn(BaseModel):
    collection_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


@app.get("/api/brands")
async def list_brands(
    scope: Scope = Depends(workspace_scope), session: AsyncSession = Depends(db)
) -> dict[str, Any]:
    rows = (
        await session.execute(
            text(
                "SELECT collection_id, name, created_at FROM brands "
                "WHERE workspace_id = :workspace ORDER BY name"
            ),
            {"workspace": scope.workspace_id},
        )
    ).all()
    return {
        "brands": [
            {"collection_id": r.collection_id, "name": r.name, "created_at": r.created_at.isoformat()}
            for r in rows
        ]
    }


@app.post("/api/brands", status_code=201)
async def create_brand(
    payload: BrandIn,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, str]:
    await session.execute(
        text(
            "INSERT INTO brands (workspace_id, collection_id, name) VALUES (:workspace, :collection, :name) "
            "ON CONFLICT (workspace_id, collection_id) DO UPDATE SET name = EXCLUDED.name"
        ),
        {"workspace": scope.workspace_id, "collection": payload.collection_id, "name": payload.name},
    )
    await session.commit()
    return {"collection_id": payload.collection_id}


# --------------------------- clusters & collections ---------------------------


class CollectionIn(BaseModel):
    name: str = Field(min_length=1)
    collection_id: str | None = None
    cluster_id: str | None = None
    description: str | None = None


class ClusterIn(BaseModel):
    name: str = Field(min_length=1)
    cluster_id: str | None = None


class CollectionRename(BaseModel):
    name: str = Field(min_length=1)


@app.get("/api/clusters")
async def clusters(
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """The whole tree — clusters, their collections, and live item counts."""
    return {"clusters": await list_clusters(session, str(principal["company_id"]))}


@app.post("/api/clusters", status_code=201)
async def add_cluster(
    payload: ClusterIn,
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    require_write(principal)
    try:
        created = await create_cluster(
            session, str(principal["company_id"]), payload.name, payload.cluster_id
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await session.commit()
    return created


@app.post("/api/collections", status_code=201)
async def add_collection(
    payload: CollectionIn,
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Create a collection. Its embedding model and dimensions are fixed now,
    because changing either later invalidates every vector inside it."""
    require_write(principal)
    try:
        created = await create_collection(
            session,
            str(principal["company_id"]),
            payload.name,
            collection_id=payload.collection_id,
            cluster_id=payload.cluster_id,
            description=payload.description,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await session.commit()
    return created


@app.get("/api/collections/{collection_id}")
async def collection_detail(
    collection_id: str,
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    enforce_binding(principal, collection_id)
    found = await get_collection(session, str(principal["company_id"]), collection_id)
    if found is None:
        raise HTTPException(404, "No such collection.")
    return found


@app.patch("/api/collections/{collection_id}")
async def edit_collection(
    collection_id: str,
    payload: CollectionRename,
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Rename a collection. Only the display name — the id is its stable handle
    and is fixed at creation."""
    require_write(principal)
    enforce_binding(principal, collection_id)
    renamed = await rename_collection(
        session, str(principal["company_id"]), collection_id, payload.name
    )
    if renamed is None:
        raise HTTPException(404, "No such collection.")
    await session.commit()
    return renamed


@app.get("/api/collections/{collection_id}/graph")
async def collection_shape(
    collection_id: str,
    k: int = Query(3, ge=1, le=8),
    limit: int = Query(200, ge=10, le=400),
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """The collection as a neighbour graph: points joined to their nearest
    neighbours by cosine similarity, plus any relationships the store recorded.

    Similarity edges are computed from the embeddings, so this is the actual
    shape of the data rather than a diagram of it.
    """
    enforce_binding(principal, collection_id)
    scope = Scope(workspace_id=str(principal["company_id"]), collection_id=collection_id)
    return await collection_graph(session, scope, k=k, limit=limit)


@app.get("/api/collections/{collection_id}/consolidation")
async def consolidation_history(
    collection_id: str,
    limit: int = Query(20, ge=1, le=100),
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """What the self-organising pass has been doing to this collection.

    Counts per run, not a status light: merging replaces passages with
    model-written text, so "it ran" is not enough — how much it changed, and
    whether it failed, is the part worth seeing.
    """
    enforce_binding(principal, collection_id)
    scope = Scope(workspace_id=str(principal["company_id"]), collection_id=collection_id)
    return {
        "enabled": consolidation_enabled(),
        "interval_seconds": consolidation_interval(),
        "runs": await recent_runs(session, scope, limit=limit),
    }


@app.post("/api/collections/{collection_id}/consolidate")
async def consolidate_now(
    collection_id: str,
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Run one consolidation pass immediately.

    Available whether or not the cron is enabled, so the behaviour can be
    tried on a collection deliberately before being left to run unattended.
    """
    require_write(principal)
    enforce_binding(principal, collection_id)
    scope = Scope(workspace_id=str(principal["company_id"]), collection_id=collection_id)
    outcome = await run_consolidation(session, scope)
    return outcome.as_dict()


@app.get("/api/chunks/{chunk_id}")
async def read_chunk(
    chunk_id: str,
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """One passage, in full, with the document it belongs to.

    What the graph opens when a point is clicked. A point on a chart that
    cannot tell you what it represents is decoration; this is what makes it an
    index you can read.
    """
    workspace = str(principal["company_id"])
    row = (
        await session.execute(
            text(
                """
                SELECT c.chunk_id, c.item_id, c.ordinal, c.heading, c.text,
                       c.node_type, c.stage, c.importance, c.archived_at,
                       c.merged_from, i.title AS document, i.source, i.locator, i.url
                FROM kb_chunks c
                LEFT JOIN kb_items i
                       ON i.item_id = c.item_id AND i.workspace_id = c.workspace_id
                      AND i.status = 'active'
                WHERE c.workspace_id = :w AND c.chunk_id = :c
                """
            ),
            {"w": workspace, "c": chunk_id},
        )
    ).first()
    if row is None:
        raise HTTPException(404, "No such passage in this workspace.")

    return {
        "chunk_id": row.chunk_id,
        "item_id": row.item_id,
        "ordinal": row.ordinal,
        "heading": row.heading,
        "text": row.text,
        "document": row.document,
        "source": row.source,
        "locator": row.locator,
        "url": row.url,
        # A summary node is text the store wrote, not text from a document.
        # The reader has to be able to tell those apart at a glance.
        "node_type": row.node_type,
        "stage": row.stage,
        "importance": float(row.importance),
        "archived": row.archived_at is not None,
        "merged_from": row.merged_from or [],
    }


@app.get("/api/chunks/{chunk_id}/lineage")
async def chunk_sources(
    chunk_id: str,
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """What a summary node was built from.

    A summary is text a model wrote, so this is the difference between a
    memory and an assertion: it names the passages behind the claim, and they
    remain resolvable after they are archived.
    """
    scope = Scope(workspace_id=str(principal["company_id"]))
    sources = await graph_lineage(scope, chunk_id)
    hydrated = await chunks_by_ids(session, scope, [s["chunk_id"] for s in sources])
    return {
        "chunk_id": chunk_id,
        "sources": [
            {
                "chunk_id": s["chunk_id"],
                "heading": s["heading"],
                "item_id": s["item_id"],
                "archived": s["archived"] is not None,
                "text": (hydrated[s["chunk_id"]].text if s["chunk_id"] in hydrated else None),
            }
            for s in sources
        ],
    }


@app.delete("/api/collections/{collection_id}")
async def drop_collection(
    collection_id: str,
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Delete a collection and its contents, reporting how much went."""
    require_write(principal)
    enforce_binding(principal, collection_id)
    removed = await delete_collection(session, str(principal["company_id"]), collection_id)
    await session.commit()
    return {"deleted": True, "items_removed": removed}


# ---------------------------------- keys ----------------------------------


class KeyIn(BaseModel):
    name: str = Field(min_length=1)
    scopes: str = "read,write"
    # None -> workspace-wide (the console default). A value binds the key to one
    # collection and is verified against the workspace before the key is minted.
    collection_id: str | None = None


@app.get("/api/keys")
async def keys(
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Existing keys — prefixes and usage only. The keys themselves are gone."""
    return {"keys": await list_keys(session, str(principal["company_id"]))}


@app.post("/api/keys", status_code=201)
async def add_key(
    payload: KeyIn,
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Issue a key. The plaintext is in this response and nowhere else, ever."""
    require_write(principal)
    try:
        created = await create_key(
            session,
            str(principal["company_id"]),
            payload.name,
            created_by=str(principal.get("email") or ""),
            scopes=payload.scopes,
            collection_id=payload.collection_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await session.commit()
    return created


@app.delete("/api/keys/{key_id}")
async def drop_key(
    key_id: str,
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, bool]:
    require_write(principal)
    if not await revoke_key(session, str(principal["company_id"]), key_id):
        raise HTTPException(404, "No such key, or it is already revoked.")
    await session.commit()
    return {"revoked": True}


@app.get("/api/snippets")
async def code_snippets(
    collection: str = Query("default", min_length=1),
    principal: dict[str, Any] = Depends(resolve_caller),
) -> dict[str, Any]:
    """Ready-to-paste code for this collection.

    The key is never interpolated — snippets get copied into chat, tickets and
    screenshots, so they read it from the environment instead.
    """
    return build_snippets(os.getenv("PUBLIC_API_URL", "http://localhost:8000"), collection)


@app.get("/api/whoami")
async def whoami(principal: dict[str, Any] = Depends(resolve_caller)) -> dict[str, Any]:
    """What this credential is. Useful when wiring up an SDK or MCP client."""
    return {
        "workspace_id": principal["company_id"],
        "identified_as": principal.get("email"),
        "via": principal.get("via"),
        "scopes": principal.get("scopes", []),
    }


# ---------------------------------- health ----------------------------------


@app.get("/api/health")
async def health() -> dict[str, Any]:
    async with Session() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok", "service": "knowledge-base", "env": os.getenv("ENV", "dev")}
