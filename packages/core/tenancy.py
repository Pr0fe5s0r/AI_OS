from __future__ import annotations

from typing import Any

from fastapi import Cookie, Depends, Header, HTTPException
from sqlalchemy import text

from packages.core.auth import resolve_session
from packages.core.db import Session
from packages.shared.schema import Scope

# Multi-tenancy: every request is scoped to one company_id, and that id comes
# from the SESSION — never from the request.
#
# It used to come from a query parameter. That was honest while there was no
# login (nothing was being protected, and pretending otherwise would have been
# worse), but the moment identity exists it becomes a hole: `?company_id=` would
# let anyone read any workspace by guessing its name. Reading it from the
# session closes that by construction rather than by remembering to check.
#
# Every endpoint already depends on company_scope, so switching the source here
# secures all of them at once — and any new endpoint that forgets to scope
# simply has no company to work with.

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


async def company_scope(principal: dict[str, Any] = Depends(current_principal)) -> str:
    return str(principal["company_id"])


async def tenant_scope(
    principal: dict[str, Any] = Depends(current_principal),
    x_brand_id: str | None = Header(default=None),
) -> Scope:
    """The two-level scope every KB call runs inside: agency, then client brand.

    The tenant still comes from the session and never from the request. The
    brand may be named by the caller — an operator legitimately switches
    between the brands they work on — but it is VERIFIED against that tenant
    before it is trusted. Without this check `X-Brand-Id` would be exactly the
    hole `?company_id=` used to be: a header that reads another client's
    content by guessing its name.

    Omitting the header scopes to the whole agency, which is the correct
    default for an operator looking across their clients.
    """
    tenant_id = str(principal["company_id"])
    if x_brand_id is None:
        return Scope(tenant_id=tenant_id)

    async with Session() as session:
        known = (
            await session.execute(
                text(
                    "SELECT 1 FROM brands WHERE tenant_id = :tenant AND brand_id = :brand LIMIT 1"
                ),
                {"tenant": tenant_id, "brand": x_brand_id},
            )
        ).first()
    if known is None:
        raise HTTPException(404, "Unknown brand for this workspace.")
    return Scope(tenant_id=tenant_id, brand_id=x_brand_id)
