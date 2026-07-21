from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.schema import Choice, DeliveryReceipt, Evidence, Situation

# A situation is a live assertion: "this rule fires on this event, right now".
# When the rule stops firing (the issue was closed, the PR got linked), the
# situation must be retired — otherwise the feed shows work that is already done.
#
# kind="clarification" situations carry `choices` instead of a recommended
# move: a genuine fork the user must pick, not a nudge. mark_resolved() is
# the ONE place a clarification's answer is recorded.

_COLUMNS = (
    "id, company_id, rule, severity, title, summary, recommended_action, evidence, "
    "status, created_at, resolved_at, kind, choices, resolved_choice, resolved_by, snoozed_until"
)

_UPSERT = text(
    """
    INSERT INTO situations
        (id, company_id, rule, severity, title, summary, recommended_action,
         evidence, status, created_at, resolved_at, kind, choices)
    VALUES
        (:id, :company_id, :rule, :severity, :title, :summary, :recommended_action,
         CAST(:evidence AS jsonb), :status, :created_at, NULL, :kind, CAST(:choices AS jsonb))
    ON CONFLICT (id) DO UPDATE SET
        severity = EXCLUDED.severity,
        summary = EXCLUDED.summary,
        recommended_action = EXCLUDED.recommended_action,
        evidence = EXCLUDED.evidence,
        -- a previously-resolved situation that fires again must reopen
        status = EXCLUDED.status,
        resolved_at = NULL,
        kind = EXCLUDED.kind,
        choices = EXCLUDED.choices
    """
)


def _situation_from_row(r: Any) -> Situation:
    ev = r.evidence if isinstance(r.evidence, list) else json.loads(r.evidence)
    choices_raw = r.choices if (r.choices is None or isinstance(r.choices, list)) else json.loads(r.choices)
    return Situation(
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
        kind=r.kind,
        choices=[Choice(**c) for c in choices_raw] if choices_raw else None,
        resolved_choice=r.resolved_choice,
        resolved_by=r.resolved_by,
        snoozed_until=r.snoozed_until,
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
            "kind": s.kind,
            "choices": json.dumps([c.model_dump(mode="json") for c in s.choices]) if s.choices else None,
        },
    )


async def is_snoozed(session: AsyncSession, company_id: str, situation_id: str) -> bool:
    """Whether an existing situation's snooze is still in effect — a detector
    must not re-fire (and thereby reopen) a situation the user snoozed."""
    row = (
        await session.execute(
            text("SELECT snoozed_until FROM situations WHERE company_id = :c AND id = :i"),
            {"c": company_id, "i": situation_id},
        )
    ).first()
    if row is None or row.snoozed_until is None:
        return False
    return row.snoozed_until > datetime.now(UTC)


async def mark_resolved(
    session: AsyncSession, company_id: str, situation_id: str, choice_id: str, resolved_by: str
) -> Situation | None:
    """Record a clarification's answer. Returns the chosen Choice's effect
    wrapped in the updated Situation, or None if the situation/choice is invalid.

    This ONLY records the answer — dispatching what the choice's `effect`
    actually does (reset a norm, disable a source, snooze) is the caller's
    job (apps/common/clarifications.py), same separation act.py uses between
    "record the decision" and "execute the move".
    """
    situation = await get_situation(session, company_id, situation_id)
    if situation is None or situation.kind != "clarification" or not situation.choices:
        return None
    if not any(c.id == choice_id for c in situation.choices):
        return None

    await session.execute(
        text(
            """
            UPDATE situations
            SET status = 'resolved', resolved_at = now(),
                resolved_choice = :choice, resolved_by = :by
            WHERE company_id = :c AND id = :i
            """
        ),
        {"c": company_id, "i": situation_id, "choice": choice_id, "by": resolved_by},
    )
    return await get_situation(session, company_id, situation_id)


async def set_snoozed_until(
    session: AsyncSession, company_id: str, situation_id: str, until: datetime
) -> None:
    await session.execute(
        text("UPDATE situations SET snoozed_until = :u WHERE company_id = :c AND id = :i"),
        {"c": company_id, "i": situation_id, "u": until},
    )


async def resolve_stale(
    session: AsyncSession, company_id: str, active_ids: list[str], kind: str | None = None
) -> int:
    """Retire every situation that did NOT fire in this detection pass.

    ``active_ids`` is the set of situation ids the rules just produced. Anything
    else is no longer true — the underlying issue was closed, assigned, labelled,
    or the PR got linked — so it is marked resolved. Passing an empty list
    resolves everything (nothing is firing any more).

    ``kind`` scopes retirement to one situation kind (e.g. "system") so a
    connector-health pass never touches business situations, and vice versa.
    """
    clauses = ["company_id = :c", "status <> 'resolved'", "NOT (id = ANY(:ids))"]
    params: dict = {"c": company_id, "ids": active_ids}
    if kind is not None:
        clauses.append("kind = :kind")
        params["kind"] = kind
    result = await session.execute(
        text(f"UPDATE situations SET status = 'resolved', resolved_at = now() WHERE {' AND '.join(clauses)}"),
        params,
    )
    return int(cast(CursorResult, result).rowcount or 0)


async def ack_situation(session: AsyncSession, company_id: str, situation_id: str) -> bool:
    """Acknowledge: someone has seen this and is on it — doesn't retire it,
    just changes how the feed presents it. Returns False if there was
    nothing open/delivered to acknowledge (already resolved, or unknown id)."""
    result = await session.execute(
        text(
            """
            UPDATE situations SET status = 'acknowledged'
            WHERE company_id = :c AND id = :i AND status IN ('open', 'delivered')
            """
        ),
        {"c": company_id, "i": situation_id},
    )
    return bool(cast(CursorResult, result).rowcount)


async def dismiss_situation(session: AsyncSession, company_id: str, situation_id: str) -> bool:
    """A human says this doesn't need action. Resolves it the same way the
    watcher retiring it would — if the underlying condition is still true
    next time the watcher engine runs, it reopens (the same "a resolved
    situation reopens if it fires again" rule as everywhere else); this is
    a dismissal, not a permanent snooze."""
    result = await session.execute(
        text(
            """
            UPDATE situations SET status = 'resolved', resolved_at = now()
            WHERE company_id = :c AND id = :i AND status <> 'resolved'
            """
        ),
        {"c": company_id, "i": situation_id},
    )
    return bool(cast(CursorResult, result).rowcount)


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
        text(f"SELECT {_COLUMNS} FROM situations WHERE company_id = :c AND id = :i"),
        {"c": company_id, "i": situation_id},
    )
    r = rows.first()
    return _situation_from_row(r) if r is not None else None


async def list_situations(
    session: AsyncSession, company_id: str, limit: int = 50, include_system: bool = False
) -> list[Situation]:
    """All situations, newest/most-severe first.

    ``system``-kind situations (the OS reporting on its own health, not the
    customer's business) are excluded unless ``include_system`` is set — there
    is no real auth in this system, so this is an honest default-visibility
    filter, not a claim of authorization.
    """
    clauses = ["company_id = :c"]
    params: dict = {"c": company_id, "l": limit}
    if not include_system:
        clauses.append("kind <> 'system'")
    rows = await session.execute(
        text(
            f"""
            SELECT {_COLUMNS}
            FROM situations WHERE {" AND ".join(clauses)}
            ORDER BY (status = 'resolved'),
                     CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                                   WHEN 'medium' THEN 2 ELSE 3 END,
                     created_at DESC
            LIMIT :l
            """
        ),
        params,
    )
    return [_situation_from_row(r) for r in rows]
