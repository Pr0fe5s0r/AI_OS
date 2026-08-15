from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from packages.core.tenancy import resolve_caller_optional

# Declared so the API documents how it is authenticated, which is not cosmetic:
# Swagger UI refuses to send an `Authorization` header that is described as an
# ordinary parameter — it sends one only through its Authorize dialog, and that
# dialog exists only when the spec declares a security scheme. Without this the
# interactive documentation could not make a single authenticated call, and
# every request from it came back 401 with the field filled in.
#
# `auto_error=False` because a bearer token is not the only credential: the
# console signs in with a cookie, and refusing a request that has no
# Authorization header would lock the browser out of its own API. The value is
# never read here — resolve_caller does the actual work — it is declared so the
# scheme reaches the spec.
_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="API key",
    description="An API key from POST /api/keys, sent as `Bearer kb_live_…`.",
)

# ---------------------------------------------------------------------------
# WHAT EACH ROUTE REQUIRES — the whole authorisation matrix, in one table.
#
# It is a table rather than a call at the top of fifty handlers for two
# reasons, and the second is the important one:
#
#   1. It can be READ. "Which routes expose document contents?" is a question
#      somebody will be asked in a security review, and the answer should be a
#      file rather than a grep.
#   2. It can be AUDITED. tests/test_scopes.py walks the running app's routes
#      and fails if any of them is missing from this table — so a route added
#      next year is a CI failure rather than a hole. Enforcement that depends
#      on the author of the next route remembering is not enforcement.
#
# The three scopes:
#
#   read     sees document contents — bodies, passages, answers, originals,
#            page pictures, and traces, which carry the query and its excerpts.
#   write    ingests and edits those contents.
#   manage   administers the CONTAINERS: creates, renames and deletes
#            collections, issues and revokes keys. It reads nothing.
#
# The separation exists so a platform operator can set a tenant up and wind
# them down without being able to read a single one of their documents.
# ---------------------------------------------------------------------------

READ = frozenset({"read"})
WRITE = frozenset({"write"})
MANAGE = frozenset({"manage"})
# Reachable with any valid credential: they disclose nothing about content.
OPEN: frozenset[str] = frozenset()

_MATRIX: dict[tuple[str, str], frozenset[str]] = {
    # --- contents -----------------------------------------------------------
    ("GET", "/api/search"): READ,
    ("GET", "/api/answer"): READ,
    ("GET", "/api/answer/stream"): READ,
    ("GET", "/api/items"): READ,
    ("GET", "/api/items/{item_id}"): READ,
    ("GET", "/api/items/{item_id}/versions"): READ,
    ("GET", "/api/items/{item_id}/related"): READ,
    ("GET", "/api/items/{item_id}/chunks"): READ,
    ("GET", "/api/items/{item_id}/structure"): READ,
    ("GET", "/api/items/{item_id}/original"): READ,
    ("GET", "/api/items/{item_id}/pages/{page}"): READ,
    ("GET", "/api/items/{item_id}/pages/{page}/figures"): READ,
    ("POST", "/api/items/batch"): READ,
    ("GET", "/api/chunks/{chunk_id}"): READ,
    ("GET", "/api/chunks/{chunk_id}/lineage"): READ,
    ("GET", "/api/chunks/{chunk_id}/neighbors"): READ,
    ("GET", "/api/facets"): READ,
    ("GET", "/api/review"): READ,
    ("GET", "/api/snippets"): READ,
    ("GET", "/api/classes"): READ,
    # A trace holds the question asked and excerpts of the passages that
    # answered it. Anyone who can read traces can read the documents, one query
    # at a time, which is why these are not management telemetry.
    ("GET", "/api/traces"): READ,
    ("GET", "/api/traces/stats"): READ,
    ("GET", "/api/traces/{trace_id}"): READ,
    # Summaries and the index graph are model-written, but they are written
    # FROM the contents and describe them closely enough to be them.
    ("GET", "/api/collections/{collection_id}/graph"): READ,
    ("GET", "/api/collections/{collection_id}/summaries"): READ,
    ("GET", "/api/collections/{collection_id}/summaries/coverage"): READ,
    ("GET", "/api/collections/{collection_id}/mapping"): READ,
    ("GET", "/api/collections/{collection_id}/consolidation"): READ,
    # --- changing contents --------------------------------------------------
    ("POST", "/api/items"): WRITE,
    ("POST", "/api/items/file"): WRITE,
    ("PATCH", "/api/items/{item_id}"): WRITE,
    ("POST", "/api/classes"): WRITE,
    ("DELETE", "/api/classes/{class_id}"): WRITE,
    ("PUT", "/api/items/{item_id}/classes"): WRITE,
    ("POST", "/api/collections/{collection_id}/consolidate"): WRITE,
    ("POST", "/api/collections/{collection_id}/summarize"): WRITE,
    # --- administering the containers ---------------------------------------
    ("POST", "/api/collections"): MANAGE,
    ("PATCH", "/api/collections/{collection_id}"): MANAGE,
    ("DELETE", "/api/collections/{collection_id}"): MANAGE,
    # Name, counts and size. Deliberately no titles: a filename is tenant
    # information — "Acme-Q3-layoffs.pdf" says something before it is opened —
    # and an operator who is not allowed to read documents is not allowed to
    # read what they are called.
    ("GET", "/api/collections/{collection_id}"): MANAGE,
    ("GET", "/api/brands"): MANAGE,
    ("POST", "/api/brands"): MANAGE,
    ("GET", "/api/clusters"): MANAGE,
    ("POST", "/api/clusters"): MANAGE,
    ("GET", "/api/keys"): MANAGE,
    ("POST", "/api/keys"): MANAGE,
    ("DELETE", "/api/keys/{key_id}"): MANAGE,
    # Membership is administration of who may reach the workspace at all, and
    # it already carries its own role check (only INVITE_ROLES may invite).
    ("GET", "/api/team"): MANAGE,
    ("POST", "/api/team/invite"): MANAGE,
    # Accepting is done BY the invited person, on their own behalf, against an
    # invitation someone with authority already issued. Requiring manage here
    # would mean only an administrator could accept their own invitation, which
    # is nobody's idea of an invitation.
    ("POST", "/api/team/accept"): OPEN,
    # --- discloses nothing --------------------------------------------------
    ("GET", "/api/health"): OPEN,
    ("GET", "/api/whoami"): OPEN,
    ("GET", "/api/formats"): OPEN,
}

# Signing in cannot require being signed in.
_UNAUTHENTICATED_PREFIX = "/api/auth/"


def requirement(method: str, path: str) -> frozenset[str] | None:
    """What this route needs, or None if the table has never heard of it."""
    return _MATRIX.get((method.upper(), path))


def matrix() -> dict[tuple[str, str], frozenset[str]]:
    """A copy, for the audit test. Never mutated in place by a caller."""
    return dict(_MATRIX)


async def authorise(
    request: Request,
    principal: dict[str, Any] | None = Depends(resolve_caller_optional),
    _bearer_declared: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, Any] | None:
    """Refuse the request unless the caller's scopes cover this route.

    Registered as an application-wide dependency, so it runs for every route
    including ones nobody remembered to think about. Dependencies resolve after
    routing, so the matched route's path TEMPLATE is available here — which is
    what makes one table possible instead of fifty decorators.

    Unknown routes are refused. That is the direction this has to fail: a route
    absent from the table is one nobody has decided about, and serving it would
    make the default "anyone may" — the exact fail-open behaviour that made a
    scoped key meaningless before any of this existed.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path is None:
        # No route matched; FastAPI will answer 404 on its own.
        return principal
    if path.startswith(_UNAUTHENTICATED_PREFIX):
        return principal

    needed = requirement(request.method, path)
    if needed is None:
        raise HTTPException(
            500,
            f"{request.method} {path} declares no scope requirement. Add it to "
            "apps/api/authz.py before serving it.",
        )
    if not needed:
        # No scope required means no CREDENTIAL required. Asking for one made
        # /api/health answer 401, which is not a policy decision — it is a
        # container reporting itself unhealthy and an orchestrator refusing to
        # start it.
        return principal
    if principal is None:
        raise HTTPException(
            401, "Not signed in. Provide a session cookie or an API key."
        )

    held = set(principal.get("scopes") or ())
    if not needed & held:
        wanted = " or ".join(sorted(needed))
        raise HTTPException(403, f"This credential needs the {wanted} scope.")
    return principal


__all__ = ["MANAGE", "OPEN", "READ", "WRITE", "authorise", "matrix", "requirement"]
