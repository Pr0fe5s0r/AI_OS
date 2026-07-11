from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.schema import Ticket

# Generic work-item store. The core neither knows nor cares what a ticket means
# to a vertical — it just holds "somebody owes this piece of work".


async def create_ticket(session: AsyncSession, ticket: Ticket) -> int:
    row = await session.execute(
        text(
            """
            INSERT INTO tickets
                (company_id, situation_id, title, description, assignee,
                 status, source_event_id, external_url)
            VALUES (:c, :sid, :t, :d, :a, :st, :ev, :url)
            RETURNING id
            """
        ),
        {
            "c": ticket.company_id, "sid": ticket.situation_id, "t": ticket.title,
            "d": ticket.description, "a": ticket.assignee, "st": ticket.status,
            "ev": ticket.source_event_id, "url": ticket.external_url,
        },
    )
    return int(row.scalar_one())


def _row_to_ticket(r) -> Ticket:
    return Ticket(
        id=r.id, company_id=r.company_id, situation_id=r.situation_id, title=r.title,
        description=r.description, assignee=r.assignee, status=r.status,
        source_event_id=r.source_event_id, external_url=r.external_url,
        created_at=r.created_at, closed_at=r.closed_at,
    )


_SELECT = """
    SELECT id, company_id, situation_id, title, description, assignee, status,
           source_event_id, external_url, created_at, closed_at
    FROM tickets
"""


async def get_ticket(session: AsyncSession, company_id: str, ticket_id: int) -> Ticket | None:
    row = (
        await session.execute(
            text(f"{_SELECT} WHERE company_id = :c AND id = :i"),
            {"c": company_id, "i": ticket_id},
        )
    ).first()
    return _row_to_ticket(row) if row else None


async def list_tickets(
    session: AsyncSession, company_id: str, assignee: str | None = None
) -> list[Ticket]:
    clause = " WHERE company_id = :c" + (" AND assignee = :a" if assignee else "")
    params: dict = {"c": company_id}
    if assignee:
        params["a"] = assignee
    rows = await session.execute(
        text(f"{_SELECT}{clause} ORDER BY (status = 'done'), created_at DESC"), params
    )
    return [_row_to_ticket(r) for r in rows]


async def close_ticket(session: AsyncSession, company_id: str, ticket_id: int) -> Ticket | None:
    await session.execute(
        text(
            """
            UPDATE tickets SET status = 'done', closed_at = :t
            WHERE company_id = :c AND id = :i AND status <> 'done'
            """
        ),
        {"c": company_id, "i": ticket_id, "t": datetime.now(UTC)},
    )
    return await get_ticket(session, company_id, ticket_id)


async def ticket_for_situation(
    session: AsyncSession, company_id: str, situation_id: str
) -> Ticket | None:
    row = (
        await session.execute(
            text(f"{_SELECT} WHERE company_id = :c AND situation_id = :s LIMIT 1"),
            {"c": company_id, "s": situation_id},
        )
    ).first()
    return _row_to_ticket(row) if row else None
