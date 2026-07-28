from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from arq import create_pool
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.auth_routes import router as auth_router
from packages.core import graph
from packages.core.classify import (
    bulk_override,
    classes_for,
    create_class,
    delete_class,
    list_classes,
    needs_review,
    override,
)
from packages.core.db import Session
from packages.core.normalise import supported
from packages.core.pipeline import redis_settings
from packages.core.search import RetrievalConfig, search
from packages.core.store import get_item, item_versions, list_items
from packages.core.tenancy import current_principal, tenant_scope
from packages.shared.schema import Lifecycle, Scope

# ---------------------------------------------------------------------------
# The Knowledge Base API. Two contracts and nothing else of consequence:
#
#   POST /api/items    the ingest contract    — one write path for everything
#   GET  /api/search   the retrieval contract — one read path for every agent
#
# Both are scoped by the caller's session (agency) plus an optional verified
# brand header. No route accepts a tenant id as a parameter.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    await graph.bootstrap()
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
async def ingest_text_item(payload: TextIngest, scope: Scope = Depends(tenant_scope)) -> Accepted:
    """Write text into the KB. Returns immediately — indexing never blocks."""
    job = await app.state.queue.enqueue_job(
        "ingest_text",
        scope.tenant_id,
        scope.brand_id,
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
    scope: Scope = Depends(tenant_scope),
) -> Accepted:
    """Upload a document. The bytes become Markdown and are then discarded.

    Locator defaults to the filename, so re-uploading the same document
    updates it rather than creating a second copy of it.
    """
    filename = file.filename or "upload"
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")

    job = await app.state.queue.enqueue_job(
        "ingest_file",
        scope.tenant_id,
        scope.brand_id,
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
    period_from: datetime | None = None,
    period_to: datetime | None = None,
    include_superseded: bool = False,
    scope: Scope = Depends(tenant_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """The single read path. Every agent uses this; behaviour comes from config.

    Results carry provenance — source, locator and link — which is what a
    citation is rendered from.
    """
    cfg = RetrievalConfig(
        limit=limit,
        min_score=min_score,
        sources=tuple(sources or ()),
        period_from=period_from,
        period_to=period_to,
        include_superseded=include_superseded,
    )
    hits = await search(session, scope, q, cfg)
    # Classes ride along so a result can be shown filed, without a call per hit.
    tagged = await classes_for(session, scope, [h.item_id for h in hits])
    return {
        "query": q,
        "count": len(hits),
        "results": [
            {**h.model_dump(), "classes": tagged.get(h.item_id, [])} for h in hits
        ],
    }


# -------------------------------- the index --------------------------------


@app.get("/api/items")
async def catalogue(
    source: str | None = None,
    status: Lifecycle = Lifecycle.ACTIVE,
    class_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    scope: Scope = Depends(tenant_scope),
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
    scope: Scope = Depends(tenant_scope), session: AsyncSession = Depends(db)
) -> dict[str, Any]:
    """Counts for the filter rail: how many items per source and per class."""
    by_source = (
        await session.execute(
            text(
                "SELECT source, count(*) AS n FROM kb_items "
                "WHERE tenant_id = :t AND status = 'active' GROUP BY source ORDER BY n DESC"
            ),
            {"t": scope.tenant_id},
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
                     AND (c.tenant_id IS NULL OR c.tenant_id = ic.tenant_id)
                WHERE ic.tenant_id = :t
                GROUP BY ic.class_id, c.name ORDER BY n DESC
                """
            ),
            {"t": scope.tenant_id},
        )
    ).all()
    total = (
        await session.execute(
            text(
                "SELECT count(*) FROM kb_items WHERE tenant_id = :t AND status = 'active'"
            ),
            {"t": scope.tenant_id},
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
    scope: Scope = Depends(tenant_scope), session: AsyncSession = Depends(db)
) -> dict[str, Any]:
    """The taxonomy this tenant can file into: platform classes plus their own."""
    return {"classes": await list_classes(session, scope)}


@app.post("/api/classes", status_code=201)
async def add_class(
    payload: ClassIn,
    scope: Scope = Depends(tenant_scope),
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
    scope: Scope = Depends(tenant_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, bool]:
    """Remove one of this tenant's own classes. System classes may be extended
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
    scope: Scope = Depends(tenant_scope),
    session: AsyncSession = Depends(db),
    principal: dict[str, Any] = Depends(current_principal),
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
    scope: Scope = Depends(tenant_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """What the KB could not file confidently — surfaced, never buried."""
    return {"items": await needs_review(session, scope, limit=limit)}


@app.get("/api/items/{item_id}")
async def one_item(
    item_id: str,
    version: int | None = None,
    scope: Scope = Depends(tenant_scope),
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
    scope: Scope = Depends(tenant_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """The lineage of an item, newest first."""
    history = await item_versions(session, scope, item_id)
    if not history:
        raise HTTPException(404, "No such item.")
    return {"item_id": item_id, "versions": [i.model_dump() for i in history]}


@app.get("/api/items/{item_id}/related")
async def related_items(
    item_id: str, hops: int = Query(1, ge=1, le=3), scope: Scope = Depends(tenant_scope)
) -> dict[str, Any]:
    """What this item connects to — lineage and derivation."""
    return (await graph.related(scope, item_id, hops=hops)).model_dump()


# ---------------------------------- brands ----------------------------------


class BrandIn(BaseModel):
    brand_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


@app.get("/api/brands")
async def list_brands(
    scope: Scope = Depends(tenant_scope), session: AsyncSession = Depends(db)
) -> dict[str, Any]:
    rows = (
        await session.execute(
            text(
                "SELECT brand_id, name, created_at FROM brands "
                "WHERE tenant_id = :tenant ORDER BY name"
            ),
            {"tenant": scope.tenant_id},
        )
    ).all()
    return {
        "brands": [
            {"brand_id": r.brand_id, "name": r.name, "created_at": r.created_at.isoformat()}
            for r in rows
        ]
    }


@app.post("/api/brands", status_code=201)
async def create_brand(
    payload: BrandIn,
    scope: Scope = Depends(tenant_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, str]:
    await session.execute(
        text(
            "INSERT INTO brands (tenant_id, brand_id, name) VALUES (:tenant, :brand, :name) "
            "ON CONFLICT (tenant_id, brand_id) DO UPDATE SET name = EXCLUDED.name"
        ),
        {"tenant": scope.tenant_id, "brand": payload.brand_id, "name": payload.name},
    )
    await session.commit()
    return {"brand_id": payload.brand_id}


# ---------------------------------- health ----------------------------------


@app.get("/api/health")
async def health() -> dict[str, Any]:
    async with Session() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok", "service": "knowledge-base", "env": os.getenv("ENV", "dev")}
