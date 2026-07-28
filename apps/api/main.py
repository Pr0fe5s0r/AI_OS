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
from packages.core.db import Session
from packages.core.normalise import supported
from packages.core.pipeline import redis_settings
from packages.core.search import RetrievalConfig, search
from packages.core.store import get_item, item_versions, list_items
from packages.core.tenancy import tenant_scope
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
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
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
    return {"query": q, "count": len(hits), "results": [h.model_dump() for h in hits]}


# -------------------------------- the index --------------------------------


@app.get("/api/items")
async def catalogue(
    source: str | None = None,
    status: Lifecycle = Lifecycle.ACTIVE,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    scope: Scope = Depends(tenant_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """What the KB holds — the listing and filtering KB-1 requires."""
    items = await list_items(
        session, scope, source=source, status=status, limit=limit, offset=offset
    )
    return {"count": len(items), "items": [i.model_dump() for i in items]}


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
    return item.model_dump()


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
