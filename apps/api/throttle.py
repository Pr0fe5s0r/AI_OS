from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, HTTPException, Request

from packages.core.limits import CLASSES, Limiter, limit_for
from packages.core.tenancy import resolve_caller_optional

log = logging.getLogger("markvector.throttle")

# ---------------------------------------------------------------------------
# WHICH COST CLASS EACH ROUTE BELONGS TO.
#
# A table for the same two reasons the authorisation matrix is a table: it can
# be read, and it can be audited. tests/test_rate_limits.py walks the running
# app's routes and fails if one of them is not classified — a route added next
# year is a CI failure rather than an unmetered hole.
#
# The classes are about COST, not about permission, so they do not line up with
# read/write/manage and should not be made to. Reading a page picture is a
# cheap read; posting a file is cheap to serve and expensive afterwards.
# ---------------------------------------------------------------------------

_CLASS: dict[tuple[str, str], str] = {
    # --- cheap reads ---------------------------------------------------------
    ("GET", "/api/search"): "read",
    ("GET", "/api/items"): "read",
    ("GET", "/api/items/{item_id}"): "read",
    ("GET", "/api/items/{item_id}/versions"): "read",
    ("GET", "/api/items/{item_id}/related"): "read",
    ("GET", "/api/items/{item_id}/chunks"): "read",
    ("GET", "/api/items/{item_id}/structure"): "read",
    ("GET", "/api/items/{item_id}/original"): "read",
    ("GET", "/api/items/{item_id}/pages/{page}"): "read",
    ("GET", "/api/items/{item_id}/pages/{page}/figures"): "read",
    ("POST", "/api/items/batch"): "read",
    ("GET", "/api/chunks/{chunk_id}"): "read",
    ("GET", "/api/chunks/{chunk_id}/lineage"): "read",
    ("GET", "/api/chunks/{chunk_id}/neighbors"): "read",
    ("GET", "/api/facets"): "read",
    ("GET", "/api/review"): "read",
    ("GET", "/api/snippets"): "read",
    ("GET", "/api/classes"): "read",
    ("GET", "/api/traces"): "read",
    ("GET", "/api/traces/stats"): "read",
    ("GET", "/api/traces/{trace_id}"): "read",
    ("GET", "/api/collections/{collection_id}/graph"): "read",
    ("GET", "/api/collections/{collection_id}/summaries"): "read",
    ("GET", "/api/collections/{collection_id}/summaries/coverage"): "read",
    ("GET", "/api/collections/{collection_id}/mapping"): "read",
    ("GET", "/api/collections/{collection_id}/consolidation"): "read",
    # --- answers: reclassified per request by mode, see classify() -----------
    ("GET", "/api/answer"): "answer",
    ("GET", "/api/answer/stream"): "answer",
    # --- work that lands on a worker -----------------------------------------
    ("POST", "/api/items"): "ingest",
    ("POST", "/api/items/file"): "ingest",
    ("PATCH", "/api/items/{item_id}"): "ingest",
    ("DELETE", "/api/items/{item_id}"): "ingest",
    ("PUT", "/api/items/{item_id}/classes"): "ingest",
    ("POST", "/api/classes"): "ingest",
    ("DELETE", "/api/classes/{class_id}"): "ingest",
    # Consolidation and summarising are many model calls per document, queued.
    # They are rarer than ingest and far more expensive per call, so they are
    # metered as agentic rather than as ingest.
    ("POST", "/api/collections/{collection_id}/consolidate"): "agentic",
    ("POST", "/api/collections/{collection_id}/summarize"): "agentic",
    # --- administration ------------------------------------------------------
    ("POST", "/api/collections"): "admin",
    ("PATCH", "/api/collections/{collection_id}"): "admin",
    ("DELETE", "/api/collections/{collection_id}"): "admin",
    ("GET", "/api/collections/{collection_id}"): "admin",
    ("GET", "/api/brands"): "admin",
    ("POST", "/api/brands"): "admin",
    ("GET", "/api/clusters"): "admin",
    ("POST", "/api/clusters"): "admin",
    ("GET", "/api/keys"): "admin",
    ("POST", "/api/keys"): "admin",
    ("DELETE", "/api/keys/{key_id}"): "admin",
    ("GET", "/api/team"): "admin",
    ("POST", "/api/team/invite"): "admin",
    ("POST", "/api/team/accept"): "admin",
}

# Not metered, and each for a reason rather than by omission.
#
#   /api/health   an orchestrator polls it. Throttling the liveness probe is a
#                 way to make a healthy container report itself dead.
#   /api/whoami   one row, called once at client startup to check a key works.
#   /api/formats  a static list.
#   /api/auth/*   there is no key yet, so there is nothing to meter it on.
#                 Sign-in DOES want limiting, keyed on the client address
#                 rather than on a credential — a different mechanism, and it
#                 is not built. Noted rather than quietly treated as done.
_EXEMPT: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/health"),
        ("GET", "/api/whoami"),
        ("GET", "/api/formats"),
    }
)

_UNAUTHENTICATED_PREFIX = "/api/auth/"


def classify(method: str, path: str, mode: str | None = None) -> str | None:
    """The cost class for a route, or None when it is exempt.

    The answer routes are the reason this takes a mode. `hybrid` is one chat
    call; `agentic` is three to five round-trips and sometimes a vision call,
    which measured 38-75 seconds on this codebase. Metering them from one
    bucket would make the limit either useless for one or punitive for the
    other, and the mode is right there in the query string.
    """
    key = (method.upper(), path)
    if key in _EXEMPT or path.startswith(_UNAUTHENTICATED_PREFIX):
        return None
    cls = _CLASS.get(key)
    if cls == "answer" and (mode or "agentic") == "agentic":
        return "agentic"
    return cls


def classified() -> dict[tuple[str, str], str]:
    """A copy, for the audit test."""
    return dict(_CLASS)


def _identity(principal: dict[str, Any] | None) -> tuple[str, str] | None:
    """Who is being metered: (bucket identity, workspace).

    A key is metered as itself. A signed-in person is metered as their session
    instead — the console is a browser making many small reads, and putting it
    in the same bucket as the API key its workspace also uses would let a busy
    integration lock a human out of their own console.
    """
    if principal is None:
        return None
    workspace = str(principal.get("company_id") or "")
    key_id = principal.get("key_id")
    if key_id:
        return f"key:{key_id}", workspace
    who = principal.get("user_id") or principal.get("email") or "anonymous"
    return f"session:{who}", workspace


async def throttle(
    request: Request,
    principal: dict[str, Any] | None = Depends(resolve_caller_optional),
) -> AsyncIterator[None]:
    """Refuse a caller who is over their limit, and hold their concurrency slot
    for the life of the request.

    Registered application-wide AFTER the authorisation dependency, so an
    unauthorised request is refused on authorisation rather than being metered
    first — a 401 should not consume anybody's budget.

    A yielding dependency because the concurrency slot has to be released when
    the response finishes, including when the handler raised, and including
    when the response is a stream that runs for a minute after the handler
    returns.

    Admission only. A request already streaming is never cut off — a
    half-written answer is worse than a clean refusal, and the caller has
    already been charged for the model calls behind it.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    limiter: Limiter | None = getattr(request.app.state, "limiter", None)
    if path is None or limiter is None:
        yield
        return

    cls = classify(request.method, path, request.query_params.get("mode"))
    who = _identity(principal)
    if cls is None or who is None:
        # No class means exempt; no identity means no credential, and the
        # authorisation dependency has already decided whether that is allowed.
        yield
        return

    identity, workspace = who
    limit = limit_for(cls, principal.get("rate_limits") if principal else None)

    decision = await limiter.check(
        key_id=identity, workspace_id=workspace, cls=cls, limit=limit
    )
    if decision.degraded:
        # Allowed, but say so. A limiter that is quietly not limiting is worse
        # than one that is off, because nobody knows the ceiling is gone.
        log.warning(
            "rate limiter unavailable (%s); allowing %s %s", decision.degraded, request.method, path
        )
    if not decision.allowed:
        raise HTTPException(
            429,
            {
                "message": (
                    f"Rate limit exceeded for {cls} requests. "
                    f"Retry in {decision.retry_after}s."
                ),
                "class": cls,
                "burst_capacity": decision.limit,
                "limit_per_minute": decision.sustained,
                "retry_after": decision.retry_after,
            },
            headers=decision.headers(),
        )

    token = await limiter.take_slot(key_id=identity, cls=cls, limit=limit)
    if token is None:
        raise HTTPException(
            429,
            {
                "message": (
                    f"Too many {cls} requests in flight at once "
                    f"(limit {limit.concurrent}). Wait for one to finish."
                ),
                "class": cls,
                "concurrent_limit": limit.concurrent,
            },
            headers={"Retry-After": "5"},
        )

    request.state.rate_limit = decision
    try:
        yield
    finally:
        await limiter.release_slot(key_id=identity, cls=cls, token=token)


__all__ = ["CLASSES", "classified", "classify", "throttle"]
