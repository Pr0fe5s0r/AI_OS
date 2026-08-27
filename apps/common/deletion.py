from __future__ import annotations

from packages.core.db import Session
from packages.core.erasure import delete_company, get_deletion_request

# Data deletion (checkpoint 6, part C): the worker-job entrypoint. Thin by
# design — packages.core.erasure owns the actual fan-out and the
# deletion_requests bookkeeping; this just runs it in the background so
# POST /api/company/delete can return immediately after queuing.


async def delete_company_job(ctx, request_id: int) -> dict:
    async with Session() as session:
        req = await get_deletion_request(session, request_id)
        if req is None or req["status"] not in ("queued", "running"):
            return {"skipped": True, "request_id": request_id}
        return await delete_company(session, req["company_id"], request_id, req["audit_policy"])
