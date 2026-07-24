from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Generic workload counter. The core has no idea what "open" or "assignee" mean —
# the vertical passes a spec describing which events count as active work and
# which metadata field holds the owner.
#
# spec = {"source": "github", "type": "issue",
#         "state_field": "state", "state_value": "open",
#         "assignee_field": "assignee"}


_WORKLOAD_KEYS = ("source", "type", "state_field", "state_value", "assignee_field")


async def open_workload(session: AsyncSession, company_id: str, spec: dict) -> dict[str, int]:
    """How many pieces of active work each person currently owns.

    An incomplete spec (a workspace that hasn't told us what "active" means)
    yields an empty map — everyone reads as free — rather than a KeyError. That
    is the safe default: capacity limits simply don't bind until configured."""
    if not all(spec.get(k) for k in _WORKLOAD_KEYS):
        return {}
    rows = await session.execute(
        text(
            """
            SELECT metadata ->> :assignee_field AS owner, count(*) AS n
            FROM events
            WHERE company_id = :c
              AND source = :source
              AND type = :type
              AND metadata ->> :state_field = :state_value
              AND metadata ->> :assignee_field IS NOT NULL
            GROUP BY 1
            """
        ),
        {
            "c": company_id,
            "source": spec["source"],
            "type": spec["type"],
            "state_field": spec["state_field"],
            "state_value": spec["state_value"],
            "assignee_field": spec["assignee_field"],
        },
    )
    return {r.owner: int(r.n) for r in rows if r.owner}
