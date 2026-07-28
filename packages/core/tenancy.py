from __future__ import annotations

from typing import Any

from fastapi import Cookie, Depends, Header, HTTPException
from sqlalchemy import text

from packages.core.auth import resolve_session
from packages.core.db import Session
from packages.core.keys import resolve_key
from packages.shared.schema import Scope

# Every request is scoped to one workspace, and that id comes from the
# CREDENTIAL — never from the request body, query or path.
#
# It used to come from a query parameter. That was honest while there was no
# login, but the moment identity exists it becomes a hole: `?workspace_id=`
# would let anyone read any workspace by guessing its name. Deriving it from
# the credential closes that by construction rather than by remembering to
# check on every endpoint.
#
# There are two credentials, and exactly one rule between them: a browser
# presents a session cookie, everything else presents an API key. Both resolve
# to the same principal shape, so no endpoint needs to know which was used.

SESSION_COOKIE = "markos_session"


async def current_principal(
    markos_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    """The signed-in user, or 401. Opens its own DB session: this runs before
    the endpoint and must not depend on the endpoint's transaction."""
    async with Session() as session:
        principal = await resolve_session(session, markos_session)
        await session.commit()  # persist last_seen_at
    if principal is None:
        raise HTTPException(401, "Not signed in.")
    return principal


async def resolve_caller(
    markos_session: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Whoever is calling: a signed-in person, or a program holding a key.

    The key is checked first because a programmatic caller may also be carrying
    a stale cookie from the same browser, and the credential it explicitly
    presented is the one it means to use.
    """
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
        async with Session() as session:
            holder = await resolve_key(session, presented)
            await session.commit()  # persist last_used_at
        if holder is None:
            raise HTTPException(401, "Invalid or revoked API key.")
        return {
            "company_id": holder["workspace_id"],
            "user_id": None,
            "email": f"key:{holder['name']}",
            "key_id": holder["key_id"],
            "scopes": holder["scopes"],
            "via": "api_key",
        }

    async with Session() as session:
        principal = await resolve_session(session, markos_session)
        await session.commit()
    if principal is None:
        raise HTTPException(
            401, "Not signed in. Provide a session cookie or an API key."
        )
    return {**principal, "via": "session", "scopes": ["read", "write"]}


def require_write(principal: dict[str, Any]) -> None:
    """A read-only key must not be able to write. Checked at the edge, once."""
    if "write" not in principal.get("scopes", ["write"]):
        raise HTTPException(403, "This key is read-only.")


async def company_scope(principal: dict[str, Any] = Depends(current_principal)) -> str:
    return str(principal["company_id"])


async def workspace_scope(
    principal: dict[str, Any] = Depends(resolve_caller),
    x_collection: str | None = Header(default=None),
) -> Scope:
    """The scope every call runs inside: a workspace, and optionally one
    collection within it.

    The workspace comes from the caller's credential and never from the
    request. The collection may be named by the caller — that is the whole
    point of collections — but it is VERIFIED against that workspace before it
    is trusted. Without the check, `X-Collection` would be exactly the hole
    `?workspace_id=` would be: a header that reads someone else's data by
    guessing its name.

    Omitting the header scopes to the entire workspace, which is the right
    default for a console looking across collections.
    """
    workspace_id = str(principal["company_id"])
    if x_collection is None:
        return Scope(workspace_id=workspace_id)

    async with Session() as session:
        known = (
            await session.execute(
                text(
                    "SELECT 1 FROM collections "
                    "WHERE workspace_id = :workspace AND collection_id = :collection LIMIT 1"
                ),
                {"workspace": workspace_id, "collection": x_collection},
            )
        ).first()
    if known is None:
        raise HTTPException(404, "Unknown collection for this workspace.")
    return Scope(workspace_id=workspace_id, collection_id=x_collection)
