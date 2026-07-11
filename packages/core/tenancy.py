from __future__ import annotations

from fastapi import Query

# Multi-tenancy: every request is scoped to one company_id. In production this
# would be derived from the authenticated principal; for now it comes from the
# request but flows through ONE dependency so no endpoint can forget to scope,
# and there are no cross-company reads. Generic — lives in the core.


async def company_scope(company_id: str = Query("default", min_length=1)) -> str:
    return company_id
