from __future__ import annotations

from typing import Any

from fastapi import Cookie, Depends, HTTPException

from packages.core.auth import resolve_session
from packages.core.db import Session

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
