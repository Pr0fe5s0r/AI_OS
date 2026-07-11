from __future__ import annotations

import json
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.schema import DeliveryReceipt, Evidence, Situation

# A situation is a live assertion: "this rule fires on this event, right now".
# When the rule stops firing (the issue was closed, the PR got linked), the
# situation must be retired — otherwise the feed shows work that is already done.

_UPSERT = text(
    """
    INSERT INTO situations
        (id, company_id, rule, severity, title, summary, recommended_action,
         evidence, status, created_at, resolved_at)
    VALUES
        (:id, :company_id, :rule, :severity, :title, :summary, :recommended_action,
         CAST(:evidence AS jsonb), :status, :created_at, NULL)
    ON CONFLICT (id) DO UPDATE SET
        severity = EXCLUDED.severity,
        summary = EXCLUDED.summary,
        recommended_action = EXCLUDED.recommended_action,
        evidence = EXCLUDED.evidence,
        -- a previously-resolved situation that fires again must reopen
        status = EXCLUDED.status,
        resolved_at = NULL
    """
)


async def save_situation(session: AsyncSession, s: Situation) -> None:
    await session.execute(
        _UPSERT,
        {
            "id": s.id,
            "company_id": s.company_id,
            "rule": s.rule,
            "severity": s.severity,
            "title": s.title,
            "summary": s.summary,
            "recommended_action": s.recommended_action,
            "evidence": json.dumps([e.model_dump(mode="json") for e in s.evidence]),
            "status": s.status,
            "created_at": s.created_at,
        },
    )


async def resolve_stale(
    session: AsyncSession, company_id: str, active_ids: list[str]
) -> int:
    """Retire every situation that did NOT fire in this detection pass.

    ``active_ids`` is the set of situation ids the rules just produced. Anything
    else is no longer true — the underlying issue was closed, assigned, labelled,
    or the PR got linked — so it is marked resolved. Passing an empty list
    resolves everything (nothing is firing any more).
    """
    result = await session.execute(
        text(
            """
            UPDATE situations
            SET status = 'resolved', resolved_at = now()
            WHERE company_id = :c
              AND status <> 'resolved'
              AND NOT (id = ANY(:ids))
            """
        ),
        {"c": company_id, "ids": active_ids},
    )
    return int(cast(CursorResult, result).rowcount or 0)


async def record_delivery(session: AsyncSession, receipt: DeliveryReceipt) -> None:
    """Only claim 'delivered' when the brief actually reached someone.

    A dry run, a missing SMTP config or a bounced send must NOT leave the
    situation looking like the on-call was told.
    """
    sent = receipt.status == "sent"
    await session.execute(
        text(
            """
            UPDATE situations
            SET status = CASE WHEN CAST(:sent AS boolean) THEN 'delivered' ELSE status END,
                channel = :ch,
                recipient = :rc,
                -- the cast is required: without it asyncpg types the CASE arm as
                -- text and the UPDATE blows up, rolling back the whole run
                delivered_at = CASE WHEN CAST(:sent AS boolean)
                                    THEN CAST(:at AS timestamptz) END
            WHERE id = :id
            """
        ),
        {
            "id": receipt.situation_id, "ch": receipt.channel, "rc": receipt.recipient,
            "at": receipt.delivered_at, "sent": sent,
        },
    )


async def get_situation(
    session: AsyncSession, company_id: str, situation_id: str
) -> Situation | None:
    rows = await session.execute(
        text(
            """
            SELECT id, company_id, rule, severity, title, summary, recommended_action,
                   evidence, status, created_at, resolved_at
            FROM situations WHERE company_id = :c AND id = :i
            """
        ),
        {"c": company_id, "i": situation_id},
    )
    r = rows.first()
    if r is None:
        return None
    ev = r.evidence if isinstance(r.evidence, list) else json.loads(r.evidence)
    return Situation(
        id=r.id, company_id=r.company_id, rule=r.rule, severity=r.severity, title=r.title,
        summary=r.summary, recommended_action=r.recommended_action,
        evidence=[Evidence(**e) for e in ev], status=r.status,
        created_at=r.created_at, resolved_at=r.resolved_at,
    )


async def list_situations(session: AsyncSession, company_id: str, limit: int = 50) -> list[Situation]:
    rows = await session.execute(
        text(
            """
            SELECT id, company_id, rule, severity, title, summary, recommended_action,
                   evidence, status, created_at, resolved_at
            FROM situations WHERE company_id = :c
            ORDER BY (status = 'resolved'),
                     CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                                   WHEN 'medium' THEN 2 ELSE 3 END,
                     created_at DESC
            LIMIT :l
            """
        ),
        {"c": company_id, "l": limit},
    )
    out: list[Situation] = []
    for r in rows:
        ev = r.evidence if isinstance(r.evidence, list) else json.loads(r.evidence)
        out.append(
            Situation(
                id=r.id,
                company_id=r.company_id,
                rule=r.rule,
                severity=r.severity,
                title=r.title,
                summary=r.summary,
                recommended_action=r.recommended_action,
                evidence=[Evidence(**e) for e in ev],
                status=r.status,
                created_at=r.created_at,
                resolved_at=r.resolved_at,
            )
        )
    return out
