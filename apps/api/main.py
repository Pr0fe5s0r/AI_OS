from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from arq import create_pool
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.auth_routes import router as auth_router
from apps.api.authz import authorise
from apps.common.consolidation import enabled as consolidation_enabled
from apps.common.consolidation import interval_seconds as consolidation_interval
from apps.common.summaries import coverage as summary_coverage_for
from apps.common.summaries import missing_cards
from packages.core import audit, blobs, figures, graph, pages, tree
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
    get_collection,
    list_clusters,
    rename_collection,
)
from packages.core.consolidate import recent_runs
from packages.core.consolidate import run_once as run_consolidation
from packages.core.db import Session
from packages.core.erasure import (
    delete_collection_and_index,
    preview_collection_deletion,
    preview_item_deletion,
)
from packages.core.erasure import delete_item as delete_item_and_index
from packages.core.graph import chunk_lineage as graph_lineage
from packages.core.graph import chunk_neighbours as graph_neighbours
from packages.core.keys import Escalation, create_key, list_keys, revoke_key
from packages.core.navigator import MAX_BEHAVIOUR_CHARS
from packages.core.neighbours import collection_graph
from packages.core.normalise import can_parse, supported
from packages.core.pipeline import redis_settings
from packages.core.search import RetrievalConfig, search_traced
from packages.core.snippets import build as build_snippets
from packages.core.store import edit_item, get_item, get_items_by_ids, item_versions, list_items
from packages.core.tenancy import (
    enforce_binding,
    require_write,
    resolve_caller,
    trace_via,
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


def _run_migrations() -> None:
    """Apply every pending Alembic migration, up to head.

    Run in a worker thread by the caller: alembic/env.py drives the async engine
    with ``asyncio.run()``, which cannot be called from inside the server's
    already-running event loop. script_location is resolved absolutely so it
    does not depend on the directory the server started in.
    """
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bring the schema to head before anything serves — owned here, by the
    # server itself, so a fresh boot (in Docker or a bare `uvicorn`) is always
    # on the current schema and a new migration needs no manual step. Runs off
    # the event loop because alembic's env drives an async engine. Set
    # AUTO_MIGRATE=false where migrations are applied out of band (for instance
    # several API replicas that must not race Alembic against each other).
    if os.getenv("AUTO_MIGRATE", "true").lower() in ("1", "true", "yes"):
        await asyncio.to_thread(_run_migrations)
    await graph.bootstrap()
    await blobs.ensure_bucket()
    app.state.queue = await create_pool(redis_settings())
    yield
    await app.state.queue.close()
    await graph.close_driver()


# Authorisation is an application-wide dependency, not a call inside each
# handler. One table (apps/api/authz.py) decides what every route needs, it runs
# for routes nobody remembered to think about, and a route missing from the
# table is refused rather than served — the opposite of how this behaved before,
# when a route with no check was a route anyone could read.
app = FastAPI(
    title="Knowledge Base",
    lifespan=lifespan,
    dependencies=[Depends(authorise)],
    # Keep the key across a page reload. Without it, every reload of the
    # interactive docs silently drops the credential and the next call comes
    # back 401 with nothing on screen explaining why — which reads as the API
    # being broken rather than the documentation forgetting.
    swagger_ui_parameters={"persistAuthorization": True},
)
# Named origins, not "*": the session travels as a cookie, and a browser
# refuses a wildcard origin on any credentialed request — so "*" would not be
# permissive, it would simply break every call the UI makes.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip()
        for o in os.getenv("WEB_ORIGINS", "http://localhost:3005").split(",")
        if o.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)

# There was none. Two handlers already called ``log.exception`` — so the moment
# either of them fired, the handler itself raised NameError and a clean 500 with
# a reason turned into a bare "Internal Server Error" with nothing behind it.
# The error path was untested precisely because it is the error path.
log = logging.getLogger("markvector.api")


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
    metadata: str | None = Form(
        None,
        description=(
            'Arbitrary metadata as a JSON object, e.g. {"client":"acme",'
            '"kind":"policy"}. Retrieval can filter on it — see ?meta= on '
            "/api/search and /api/answer."
        ),
    ),
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

    # Rejected loudly rather than dropped. Metadata that silently failed to
    # attach is worse than metadata that was refused: retrieval filtered on it
    # would quietly exclude the document, and the upload said 202.
    tags: dict[str, Any] = {}
    if metadata:
        try:
            tags = json.loads(metadata)
        except ValueError as exc:
            raise HTTPException(422, f"metadata is not valid JSON: {exc}") from exc
        if not isinstance(tags, dict):
            raise HTTPException(422, "metadata must be a JSON object.")

    # An upper bound, because the whole file is in memory by the line above and
    # several API replicas each holding a large upload is how a container gets
    # OOM-killed mid-request — which the caller sees as the connection dropping,
    # with nothing in any log to explain it. Refusing is the kinder failure.
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"{filename} is {len(data) // (1024 * 1024)}MB, over the "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit. Split it, or raise "
            "MAX_UPLOAD_MB on the server.",
        )

    # Refuse here rather than in the worker. Accepting a file we have no parser
    # for produced the worst of both: the upload reported success, indexing
    # failed in a job whose reason nobody reads, and the console could only say
    # the document was "not searchable yet" — which sounds like a delay.
    if not can_parse(filename):
        raise HTTPException(
            415, f"Cannot read {filename}. Supported formats: {', '.join(supported())}"
        )

    # A big file goes to blob storage and the queue carries only its key. Redis
    # is a message broker, not a file store, and a 17 MB job payload is how you
    # find that out.
    payload_data: bytes | str = data
    if len(data) > INLINE_UPLOAD_LIMIT:
        if not blobs.enabled():
            # Said plainly instead of enqueuing it anyway. Without this the job
            # payload goes to Redis, fails somewhere inside the queue client,
            # and the console shows "Internal Server Error" against a file that
            # is simply too big for the way this deployment is configured.
            raise HTTPException(
                503,
                f"{filename} is {len(data) // (1024 * 1024)}MB. Files over "
                f"{INLINE_UPLOAD_LIMIT // (1024 * 1024)}MB need blob storage, "
                "which is not configured on this deployment. Set the S3/MinIO "
                "variables, or upload a smaller file.",
            )
        staging_key = f"staging/{scope.workspace_id}/{hashlib.sha256(data).hexdigest()}"
        content_type = (
            file.content_type
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        try:
            await blobs.put(staging_key, data, content_type)
        except Exception as exc:
            # This call used to sit outside any try. A blob store that was full,
            # unreachable or misconfigured raised straight through the route and
            # the browser got a bare 500 with nothing in it — no filename, no
            # cause, nothing to act on.
            log.exception("Staging upload failed for %s", filename)
            raise HTTPException(
                502, f"Could not stage {filename} for indexing: {type(exc).__name__}"
            ) from exc
        payload_data = staging_key

    try:
        job = await app.state.queue.enqueue_job(
            "ingest_file",
            scope.workspace_id,
            scope.collection_id,
            source,
            locator or filename,
            filename,
            payload_data,
            url,
            period_start,
            period_end,
            tags or None,
        )
        return Accepted(job_id=job.job_id if job else None)
    except Exception as exc:
        log.exception("Failed to enqueue ingest_file job for %s", filename)
        raise HTTPException(500, f"Upload enqueue failed: {type(exc).__name__}") from exc


@app.get("/api/formats")
async def formats() -> dict[str, list[str]]:
    """Which file types can be ingested today."""
    return {"supported": list(supported())}


# ----------------------------- retrieval contract -----------------------------


_META_HELP = (
    "Filter by the metadata a document was ingested with, as key:value. "
    "Repeatable: ?meta=client:acme&meta=kind:policy. Different keys must all "
    "match; the same key repeated matches any of its values — so "
    "?meta=kind:policy&meta=kind:notice&meta=client:acme reads as 'client acme, "
    "and either a policy or a notice'. Values are compared as text."
)


def _metadata_pairs(raw: list[str] | None) -> tuple[tuple[str, str], ...]:
    """Parse `key:value` filters off the query string.

    Split on the FIRST colon only, so a value may contain one — a URL, a
    timestamp, a path. A pair with no colon, or with an empty key, is rejected
    rather than ignored: a filter that silently does nothing is how a caller
    ends up trusting an answer drawn from documents they meant to exclude,
    which is the one failure this feature must never have.
    """
    pairs: list[tuple[str, str]] = []
    for entry in raw or ():
        key, sep, value = entry.partition(":")
        if not sep or not key.strip():
            raise HTTPException(
                422,
                f"Metadata filter {entry!r} is not key:value. "
                "Example: ?meta=client:acme",
            )
        pairs.append((key.strip(), value))
    return tuple(pairs)


@app.get("/api/search")
async def retrieve(
    q: str = Query(min_length=1),
    limit: int = Query(10, ge=1, le=100),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    sources: list[str] | None = Query(None),
    item_ids: list[str] | None = Query(
        None, description="Restrict the search to these document ids."
    ),
    meta: list[str] | None = Query(None, description=_META_HELP),
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
        metadata=_metadata_pairs(meta),
        period_from=period_from,
        period_to=period_to,
        include_superseded=include_superseded,
    )
    hits, trace = await search_traced(session, scope, q, cfg)
    await record(
        session,
        scope,
        trace,
        via=trace_via(principal),
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


# The longest a single question may take before the caller is told it failed.
#
# Neither answer path had a bound, and one query proved what that costs: asked
# which elements are liquid, a streamed vectorless answer stayed open for over
# twelve minutes with the connection alive, no error, and a spinner that never
# stopped. The work is genuinely slow — a vision-escalated answer measured 36s
# honestly — so the bound has to sit well above that, but it has to exist.
#
# Nothing else can supply it. The provider timeout only bounds ONE call and the
# navigator makes several; and every call runs inside asyncio.to_thread, so
# cancelling the task cannot interrupt a thread parked in a socket read. This is
# the only place that can promise the caller an ending.
ANSWER_DEADLINE_SECONDS = float(os.getenv("ANSWER_DEADLINE_SECONDS", "300"))

# The largest upload accepted at all. The file is read fully into memory before
# anything else can happen, so this is a memory bound per in-flight request, not
# a policy. 128MB clears a 17MB scanned book with room to spare.
MAX_UPLOAD_BYTES = int(float(os.getenv("MAX_UPLOAD_MB", "128")) * 1024 * 1024)
# Above this, the bytes go to blob storage and the queue carries only the key.
# Redis is a message broker; a multi-megabyte job payload is how you learn that.
INLINE_UPLOAD_LIMIT = 2 * 1024 * 1024

# How long the answer stream may go quiet before it sends a keepalive comment.
# Well under the 60s idle timeout common to proxies and load balancers, and far
# under the longest measured gap between real events (42.5s for one agentic
# read). Costs four bytes.
HEARTBEAT_SECONDS = 15.0


def _too_slow() -> str:
    return (
        f"The answer took longer than {int(ANSWER_DEADLINE_SECONDS)}s and was given "
        "up on. Anything shown above was found before that. Try hybrid mode, which "
        "does not read pages as images."
    )


def _sse(event: dict[str, Any]) -> str:
    # ensure_ascii=False so accented text survives the wire as itself. Escaping
    # it is not wrong, but it makes a stored answer about Kutuzov or Helene
    # unreadable in a log and unequal to the same string read back from PG.
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get("/api/answer")
async def answer_question(
    q: str = Query(min_length=1),
    limit: int = Query(8, ge=1, le=20),
    mode: str = Query("agentic", pattern="^(hybrid|vectorless|agentic)$"),
    sources: list[str] | None = Query(None),
    doc: list[str] | None = Query(
        None,
        description=(
            "Restrict the answer to these document ids. Repeatable: "
            "?doc=a&doc=b. Omitted, the store decides which documents the "
            "question is about."
        ),
    ),
    meta: list[str] | None = Query(None, description=_META_HELP),
    vision: bool = Query(
        True,
        description=(
            "Let the agent read a page as a picture when the text layer cannot "
            "answer. Off, a figure stops being readable and a page-picture "
            "question fails honestly rather than being answered from a caption "
            "— but the single most expensive step in a walk is gone."
        ),
    ),
    behaviour: str = Query(
        "",
        max_length=MAX_BEHAVIOUR_CHARS,
        description=(
            "How the answer should be WRITTEN — tone, length, formatting. It "
            "cannot change what may be said: citing only what was read, and "
            "saying so when the documents do not answer, are not negotiable."
        ),
    ),
    open_document: bool = Query(
        True,
        description=(
            "Let the agent open a whole document's outline when the catalogue "
            "was shortened. Off, it navigates from the catalogue and the "
            "section hint alone."
        ),
    ),
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

      agentic  (default) one agent reaches the whole collection: it reasons over
               the tables of contents, runs hybrid search to locate a figure or
               identifier no heading advertises, and hops the similarity graph
               from a promising passage, then reads what it lands on
      hybrid   passage embeddings and keyword matching, fused into one ranked
               pass — fast, deterministic, the primitive other software builds on

    A third value, `vectorless`, is accepted for backward compatibility only
    (catalogue reasoning with search and graph-hop off); it is no longer a
    surfaced choice and answers from a partial view once a collection exceeds
    the catalogue window, which the response then says out loud. The mode comes
    back on the response, because two answers to one question can differ
    entirely on it.
    """
    cfg = RetrievalConfig(
        limit=limit, sources=tuple(sources or ()), metadata=_metadata_pairs(meta)
    )
    try:
        result, trace = await asyncio.wait_for(
            answer(
                session,
                scope,
                q,
                cfg,
                mode=mode,
                item_ids=tuple(doc or ()),
                vision=vision,
                behaviour=behaviour,
                allow_open=open_document,
            ),
            timeout=ANSWER_DEADLINE_SECONDS,
        )
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                f"The answer took longer than {int(ANSWER_DEADLINE_SECONDS)}s and was "
                "given up on. Try hybrid mode, which does not read pages as images."
            ),
        ) from None

    # Recorded once, under the retrieval that produced it: an answer whose
    # retrieval cannot be inspected is not one anybody can argue with.
    await record(
        session,
        scope,
        trace,
        via=trace_via(principal),
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


@app.get("/api/answer/stream")
async def answer_stream(
    q: str = Query(min_length=1),
    limit: int = Query(8, ge=1, le=20),
    mode: str = Query("agentic", pattern="^(hybrid|vectorless|agentic)$"),
    sources: list[str] | None = Query(None),
    doc: list[str] | None = Query(
        None,
        description=(
            "Restrict the answer to these document ids. Repeatable: "
            "?doc=a&doc=b. Omitted, the store decides which documents the "
            "question is about."
        ),
    ),
    meta: list[str] | None = Query(None, description=_META_HELP),
    vision: bool = Query(True, description="See /api/answer."),
    behaviour: str = Query("", max_length=MAX_BEHAVIOUR_CHARS, description="See /api/answer."),
    open_document: bool = Query(True, description="See /api/answer."),
    scope: Scope = Depends(workspace_scope),
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> StreamingResponse:
    """The same answer as ``/api/answer``, streamed as it is produced.

    Server-Sent Events: one ``data:`` line per step — the agent's reasoning as
    it arrives (``thinking``), each ``tool_call`` and its ``tool_result``, and a
    terminal ``done`` carrying exactly the payload the non-streaming route
    returns. A caller that only wants the answer keeps using ``/api/answer``;
    this exists to show the work. The trace is recorded once, at the end, under
    the retrieval that produced it — identical to the blocking route.
    """
    cfg = RetrievalConfig(
        limit=limit, sources=tuple(sources or ()), metadata=_metadata_pairs(meta)
    )

    async def events() -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def emit(event: dict[str, Any]) -> None:
            await queue.put(event)

        async def run() -> None:
            # The work runs as its own task and reports through the queue; this
            # coroutine only drains it. They share the session, but only this
            # one touches the database, so there is a single writer throughout.
            try:
                result, trace = await answer(
                    session, scope, q, cfg, mode=mode, emit=emit,
                    item_ids=tuple(doc or ()),
                    vision=vision, behaviour=behaviour,
                    allow_open=open_document,
                )
                await record(
                    session,
                    scope,
                    trace,
                    via=trace_via(principal),
                    actor=str(principal.get("email") or ""),
                )
                await session.commit()
                tagged = await classes_for(session, scope, [h.item_id for h in result.hits])
                await queue.put(
                    {
                        "type": "done",
                        "answer": {
                            **result.as_dict(),
                            "results": [
                                {**h.model_dump(), "classes": tagged.get(h.item_id, [])}
                                for h in result.hits
                            ],
                        },
                    }
                )
            except Exception as exc:
                await queue.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                await queue.put(None)

        task = asyncio.create_task(run())
        deadline = time.monotonic() + ANSWER_DEADLINE_SECONDS
        try:
            while True:
                # Waited on with a deadline rather than indefinitely. Without
                # one, an answer that never finishes is a stream that never
                # closes: the steps already sent stay on screen, the spinner
                # keeps turning, and nothing ever tells the reader it is over.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    yield _sse({"type": "error", "detail": _too_slow()})
                    break
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=min(HEARTBEAT_SECONDS, remaining)
                    )
                except TimeoutError:
                    # Nothing produced for a while — which is normal here, not a
                    # fault. A single agentic read was measured at 42.5 seconds:
                    # one model call, no bytes on the wire for its whole
                    # duration. Every proxy between here and the browser reads
                    # that silence as a dead connection and closes it, and the
                    # reader sees "network error" over an answer that was being
                    # produced perfectly well. Verified: the same question
                    # succeeded in 87.5s straight to this API and failed through
                    # the dev proxy.
                    #
                    # An SSE comment. Clients ignore it by specification, so it
                    # keeps the connection warm without appearing as an event.
                    yield ": keepalive\n\n"
                    continue
                if event is None:
                    break
                yield _sse(event)
        finally:
            # A reader who closes the tab should not leave the agent running.
            # Cancelling cannot reach a provider call already inside a thread —
            # only that call's own timeout ends it — but it does stop the loop
            # from starting another round on someone who has gone.
            #
            # Only the unfinished task is cancelled. Awaiting unconditionally is
            # what the deadline was added to escape: on the timeout path the
            # task is by definition still running, so `await task` re-blocks for
            # exactly as long as the deadline just refused to wait.
            if not task.done():
                task.cancel()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Tell nginx / any reverse proxy not to buffer — buffering an event
            # stream defeats the point, turning it back into one late response.
            "X-Accel-Buffering": "no",
        },
    )


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


@app.delete("/api/items/{item_id}")
async def remove_item(
    item_id: str,
    confirm: bool = Query(
        False,
        description=(
            "Must be true to delete. Without it the request is refused with a "
            "409 describing exactly what would be destroyed — the title, how "
            "many versions and how many passages — so a caller can show that "
            "to a person before anything happens."
        ),
    ),
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
    principal: dict[str, Any] = Depends(resolve_caller),
) -> dict[str, Any]:
    """Delete a document, its passages, its vectors and its stored original.

    Two-step by design. A DELETE without `confirm=true` deletes nothing and
    answers 409 with a summary of what it WOULD delete; the same call with
    `confirm=true` carries it out. That is not ceremony — there is no undo here
    and no trash to restore from, and "delete this document?" is a question
    nobody can answer well. "Delete COMPUTER NETWORKS, 2 versions, 3,915
    passages, permanently" is.

    Everything derived goes with it: the graph nodes and their embeddings, the
    stored original, every rendered page and every cached figure. A passage
    that outlived its document would still hold an embedding, and so would
    still answer questions — which is the one thing a deleted document must
    never do.
    """
    require_write(principal)

    summary = await preview_item_deletion(session, scope, item_id)
    if summary is None:
        raise HTTPException(404, "No such item.")

    if not confirm:
        raise HTTPException(
            409,
            {
                "message": (
                    f"This will permanently delete {summary['title']!r} — "
                    f"{summary['versions']} version(s) and {summary['passages']} "
                    "passage(s), along with the stored original, its rendered "
                    "pages and its vectors. There is no undo. Repeat the "
                    "request with ?confirm=true to proceed."
                ),
                "would_delete": summary,
            },
        )

    removed = await delete_item_and_index(session, scope, item_id)
    await audit.record(
        session,
        scope.workspace_id,
        str(principal.get("email") or "unknown"),
        "item.deleted",
        item_id,
        {"title": summary["title"], "removed": removed},
    )
    await session.commit()
    return {"deleted": True, **removed, "title": summary["title"]}


class ItemEditIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = None
    metadata: dict[str, Any] | None = None


@app.patch("/api/items/{item_id}")
async def update_item(
    item_id: str,
    payload: ItemEditIn,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
    principal: dict[str, Any] = Depends(resolve_caller),
) -> dict[str, Any]:
    """Edit user-owned document information while keeping version history."""
    if payload.title is None and payload.body is None and payload.metadata is None:
        raise HTTPException(400, "Provide a title, document text, or metadata to update.")
    try:
        item, needs_embedding = await edit_item(
            session,
            scope,
            item_id,
            title=payload.title,
            body=payload.body,
            metadata=payload.metadata,
        )
    except LookupError:
        raise HTTPException(404, "No such item.") from None

    actor = str(principal.get("email") or principal.get("user_id") or "unknown")
    await audit.record(
        session,
        scope.workspace_id,
        actor,
        "item.edited",
        item_id,
        {
            "version": item.version,
            "fields": [
                name
                for name, value in (
                    ("title", payload.title),
                    ("body", payload.body),
                    ("metadata", payload.metadata),
                )
                if value is not None
            ],
        },
    )
    await session.commit()
    if needs_embedding:
        await app.state.queue.enqueue_job(
            "embed_item", scope.workspace_id, scope.collection_id, item_id
        )
        await app.state.queue.enqueue_job(
            "classify_new_item", scope.workspace_id, scope.collection_id, item_id, None
        )
    tagged = await classes_for(session, scope, [item.id])
    return {**item.model_dump(), "classes": tagged.get(item.id, [])}


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


def _structure_of(item: Any) -> dict[str, Any]:
    """A document's own table of contents as a tree — the PageIndex structure
    vectorless retrieval reasons over. Titles, sizes and a one-line preview per
    section; deterministic and free (no model calls)."""
    root = tree.build(item.body, item.title)
    return {
        "item_id": item.id,
        "title": item.title,
        "nodes": tree.count(root),
        "sections": root.outline().get("sections", []),
    }


@app.get("/api/items/{item_id}/structure")
async def item_structure(
    item_id: str,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """The document's structure — its heading tree, the way vectorless search
    sees it. This is the index a model chooses sections from before reading."""
    item = await get_item(session, scope, item_id)
    if item is None:
        raise HTTPException(404, "No such item.")
    return _structure_of(item)


class BatchIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=100)
    # Include each document's PageIndex structure — bulk structure extraction
    # for a list of files, in one round trip.
    structure: bool = False


@app.post("/api/items/batch")
async def batch_items(
    payload: BatchIn,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Fetch many documents at once, in the order asked for. Missing ids are
    simply absent. Set `structure` to also get each one's heading tree."""
    items = await get_items_by_ids(session, scope, payload.ids)
    by_id = {i.id: i for i in items}
    ordered = [by_id[i] for i in payload.ids if i in by_id]
    tagged = await classes_for(session, scope, [i.id for i in ordered])
    out: list[dict[str, Any]] = []
    for item in ordered:
        row = {**item.model_dump(), "classes": tagged.get(item.id, [])}
        if payload.structure:
            row["structure"] = _structure_of(item)
        out.append(row)
    return {"count": len(out), "items": out}


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


@app.get("/api/items/{item_id}/pages/{page}")
async def item_page(
    item_id: str,
    page: int,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> Response:
    """One page of the original, as a picture.

    Serves the citation, not the reader's browsing: when an answer was read off
    a page rather than out of extracted text, this is the page it was read off,
    so the transcription can be checked against the thing itself. A citation
    nobody can check is the failure mode this whole store is built against, and
    a transcribed table is exactly where that matters most.

    404 for everything without a page — pasted text, Markdown, a .docx, a
    document whose original was never stored. Scoped like every other route:
    the workspace comes from the caller, never from the path.
    """
    if await get_item(session, scope, item_id) is None:
        raise HTTPException(404, "No such item.")
    if page < 1:
        raise HTTPException(422, "Pages are numbered from 1.")

    png = await pages.image(scope.workspace_id, item_id, page)
    if png is None:
        raise HTTPException(404, "That page is not available as a picture.")
    return Response(
        content=png,
        media_type="image/png",
        # Deterministic: the same page of the same file renders identically, so
        # it is worth caching hard. Private because it is workspace data.
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.get("/api/items/{item_id}/pages/{page}/figures")
async def item_page_figures(
    item_id: str,
    page: int,
    scope: Scope = Depends(workspace_scope),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Where the pictures are on this page, as boxes to crop out of it.

    Asked for by the reader AFTER an answer has arrived, against the pages that
    answer cited — never during it. That is the whole reason this is a route of
    its own rather than a field on the answer: an answer must not wait on
    anything a reader might not scroll to, and a store with no diagrams in it
    stays exactly as fast as it was.

    Empty is a perfectly good reply, and the common one. Most pages of most
    documents are words.
    """
    if await get_item(session, scope, item_id) is None:
        raise HTTPException(404, "No such item.")
    if page < 1:
        raise HTTPException(422, "Pages are numbered from 1.")
    return {
        "figures": await figures.for_page(scope.workspace_id, item_id, page),
        # Cached like the picture it crops: same file, same rules, same boxes.
        "page": page,
    }


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


# ------------------------------ index summaries ------------------------------


@app.get("/api/collections/{collection_id}/summaries")
async def collection_summaries(
    collection_id: str,
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Every index summary in this collection with coverage and provenance.

    This is the Summaries view's data source: each summary, how many chunks it
    connects, and how it was generated (at upload or by the mapper + its probe
    question).
    """
    enforce_binding(principal, collection_id)
    scope = Scope(workspace_id=str(principal["company_id"]), collection_id=collection_id)
    coverage = await graph.summary_coverage(scope)

    # Hydrate with the text + provenance fields from Postgres.
    chunk_ids = [r["chunk_id"] for r in coverage]
    if chunk_ids:
        placeholders = ", ".join(f":c{i}" for i in range(len(chunk_ids)))
        params: dict[str, Any] = {"w": scope.workspace_id}
        params.update({f"c{i}": cid for i, cid in enumerate(chunk_ids)})
        rows = (
            await session.execute(
                text(
                    f"SELECT chunk_id, item_id, heading, text, node_type, generated_by, "  # noqa: S608
                    f"probe_question FROM kb_chunks "
                    f"WHERE workspace_id = :w AND chunk_id IN ({placeholders})"
                ),
                params,
            )
        ).all()
        pg_data = {r.chunk_id: r for r in rows}
    else:
        pg_data = {}

    summaries = []
    for c in coverage:
        pg = pg_data.get(c["chunk_id"])
        summaries.append({
            "chunk_id": c["chunk_id"],
            "node_type": c.get("node_type") or (pg.node_type if pg else None),
            "item_id": c.get("item_id") or (pg.item_id if pg else None),
            "heading": c.get("heading") or (pg.heading if pg else None),
            "text": pg.text if pg else None,
            "covers": int(c.get("covers", 0)),
            "generated_by": pg.generated_by if pg else None,
            "probe_question": pg.probe_question if pg else None,
        })

    return {"summaries": summaries}


@app.get("/api/collections/{collection_id}/mapping")
async def collection_mapping(
    collection_id: str,
    limit: int = Query(20, ge=1, le=100),
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Mapping progress and recent mapper runs — the Summaries header stat.

    Shows total/mapped/unmapped counts and a live feed of the probe-mapper's
    runs (the random question it asked + chunks it mapped each pass).
    """
    enforce_binding(principal, collection_id)
    scope = Scope(workspace_id=str(principal["company_id"]), collection_id=collection_id)

    counts = await graph.mapping_counts(scope)

    runs = (
        await session.execute(
            text(
                "SELECT id, question, chunks_mapped, error, created_at "
                "FROM mapper_runs "
                "WHERE workspace_id = :w AND (collection_id = :c OR collection_id IS NULL) "
                "ORDER BY created_at DESC LIMIT :limit"
            ),
            {"w": scope.workspace_id, "c": collection_id, "limit": limit},
        )
    ).all()

    return {
        **counts,
        "runs": [
            {
                "id": r.id,
                "question": r.question,
                "chunks_mapped": r.chunks_mapped,
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in runs
        ],
    }


@app.post("/api/collections/{collection_id}/summarize")
async def summarize_now(
    collection_id: str,
    rebuild: bool = Query(
        False,
        description=(
            "Re-summarise documents that already have a card. Off by default: "
            "summarising is one model call per section plus one for the card, "
            "so a rebuild of a large collection is expensive and is almost "
            "never what you want."
        ),
    ),
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Queue index-summary generation for this collection, now.

    By default this fills GAPS — documents with no card — and leaves the rest
    alone. It used to pass force=True for every document every time, re-buying
    summaries that were already correct: on a hundred-document collection that
    is roughly 1,300 model calls to end up where it started. Pass
    ``rebuild=true`` when you actually want them regenerated (a prompt changed,
    a model changed).

    The work runs in the worker, never here: summarising is many model calls per
    document, and doing it inline would block the API for the whole collection.
    Each document is handed to the same background ``summarize_item`` job that
    runs after an upload, so a slow or failing model call can never reach — let
    alone stall — the request thread.

    A cron does this continuously (see apps/common/summaries.py); this endpoint
    exists for when you do not want to wait for it.
    """
    require_write(principal)
    enforce_binding(principal, collection_id)
    workspace = str(principal["company_id"])
    scope = Scope(workspace_id=workspace, collection_id=collection_id)

    if rebuild:
        item_rows = (
            await session.execute(
                text(
                    "SELECT item_id FROM kb_items "
                    "WHERE workspace_id = :w AND collection_id = :c AND status = 'active'"
                ),
                {"w": workspace, "c": collection_id},
            )
        ).all()
        targets = [r.item_id for r in item_rows]
    else:
        targets = await missing_cards(scope)

    for item_id in targets:
        # force=True: an explicit "generate now" runs regardless of the
        # SUMMARIES_ENABLED cron gate, like consolidate_now. Whether a document
        # NEEDED doing was already decided above.
        await app.state.queue.enqueue_job(
            "summarize_item", workspace, collection_id, item_id, True
        )

    return {"queued": len(targets), "rebuild": rebuild, **await summary_coverage_for(scope)}


@app.get("/api/collections/{collection_id}/summaries/coverage")
async def summaries_coverage(
    collection_id: str,
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """How much of this collection has a card.

    Worth its own route because it is a retrieval-quality number, not a cosmetic
    one: a document without a card competes on the weaker of the two routing
    signals, and on a store larger than the catalogue window that decides
    whether it is considered at all. Nobody should have to infer that from
    answers coming back wrong.
    """
    enforce_binding(principal, collection_id)
    scope = Scope(workspace_id=str(principal["company_id"]), collection_id=collection_id)
    return await summary_coverage_for(scope)


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


@app.get("/api/chunks/{chunk_id}/neighbors")
async def chunk_neighbors(
    chunk_id: str,
    limit: int = Query(10, ge=1, le=50),
    principal: dict[str, Any] = Depends(resolve_caller),
) -> dict[str, Any]:
    """The passages nearest this one — the graph's traversal primitive.

    Given a passage, what sits next to it in meaning: the stored :NEAR edges the
    consolidation pass maintains. This is the hop an agent takes to walk from a
    passage a search turned up to related material, following the thread rather
    than searching again from the top. The same primitive serves our own
    navigator and a caller's own-LLM agent through the SDK.
    """
    scope = Scope(workspace_id=str(principal["company_id"]))
    neighbours = await graph_neighbours(scope, chunk_id, limit=limit)
    return {
        "chunk_id": chunk_id,
        "neighbors": [
            {
                "neighbor_id": n["chunk_id"],
                "item_id": n["item_id"],
                "heading": n["heading"],
                "title": n["title"],
                "node_type": n["node_type"],
                # The edge kind: an authored relation (elaborates/defines/…) when
                # one was written, else "near" for a cosine link. `typed` says
                # which, so a caller can prefer the judged links.
                "relation": n.get("relation") or "near",
                "typed": bool(n.get("typed")),
                "similarity": (
                    round(n["similarity"], 4) if n["similarity"] is not None else None
                ),
            }
            for n in neighbours
        ],
    }


@app.delete("/api/collections/{collection_id}")
async def drop_collection(
    collection_id: str,
    principal: dict[str, Any] = Depends(resolve_caller),
    session: AsyncSession = Depends(db),
) -> dict[str, Any]:
    """Delete a collection, its documents, and everything they derived.

    Not only its rows. Every passage in the graph goes with it, and so does
    every stored original and rendered page — this used to delete the Postgres
    rows alone and leave the vectors behind, which meant a deleted collection
    carried on answering questions from passages nothing pointed at any more.

    The counts come back per store, because "deleted: true" is exactly the
    report that hides a deletion which reached only one of them.
    """
    enforce_binding(principal, collection_id)
    workspace = str(principal["company_id"])

    summary = await preview_collection_deletion(session, workspace, collection_id)
    if summary is None:
        raise HTTPException(404, "No such collection.")

    removed = await delete_collection_and_index(session, workspace, collection_id)
    await audit.record(
        session,
        workspace,
        str(principal.get("email") or "unknown"),
        "collection.deleted",
        collection_id,
        {"name": summary["name"], "removed": removed},
    )
    await session.commit()
    # items_removed keeps its meaning — kb_items rows, i.e. versions — because
    # the console and both SDKs already read it.
    return {"deleted": True, "items_removed": removed["items_removed"], **removed}


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
    try:
        created = await create_key(
            session,
            str(principal["company_id"]),
            payload.name,
            created_by=str(principal.get("email") or ""),
            scopes=payload.scopes,
            collection_id=payload.collection_id,
            # A key may only issue keys no stronger than itself. Without this a
            # manage-only operator mints themselves a read key and the whole
            # separation is one API call deep. None for a signed-in person, who
            # is bounded by their membership rather than by a key.
            minter_scopes=(
                set(principal.get("scopes") or ())
                if principal.get("via") == "api_key"
                else None
            ),
        )
    except Escalation as exc:
        raise HTTPException(403, str(exc)) from exc
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
    return build_snippets(os.getenv("PUBLIC_API_URL", "http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me"), collection)


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
