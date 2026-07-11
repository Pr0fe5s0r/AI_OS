from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Append-only audit log. Generic — every side-effecting action (ingest trigger,
# later: approvals + actions) records here, always company-scoped.

_INSERT = text(
    """
    INSERT INTO audit_log (company_id, actor, action, target, metadata)
    VALUES (:company_id, :actor, :action, :target, CAST(:metadata AS jsonb))
    """
)


async def record(
    session: AsyncSession,
    company_id: str,
    actor: str,
    action: str,
    target: str = "",
    metadata: dict | None = None,
) -> None:
    await session.execute(
        _INSERT,
        {
            "company_id": company_id,
            "actor": actor,
            "action": action,
            "target": target,
            "metadata": json.dumps(metadata or {}),
        },
    )
