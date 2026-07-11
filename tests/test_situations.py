from __future__ import annotations

from datetime import UTC, datetime

from packages.core.db import Session
from packages.core.situations import list_situations, resolve_stale, save_situation
from packages.shared.schema import Evidence, Situation

_CO = "test-situations"


def _sit(sid: str, severity: str = "high") -> Situation:
    return Situation(
        id=sid,
        company_id=_CO,
        rule="untriaged_issue",
        severity=severity,
        title="Open issue with no triage",
        summary="something",
        evidence=[Evidence(event_id=sid.split(":")[-1], source="github",
                           timestamp=datetime(2026, 7, 10, tzinfo=UTC), excerpt="x")],
        status="open",
        created_at=datetime(2026, 7, 10, tzinfo=UTC),
    )


async def test_situations_that_stop_firing_are_retired() -> None:
    a, b = _sit("untriaged_issue:a"), _sit("untriaged_issue:b")

    async with Session() as session:
        for s in (a, b):
            await save_situation(session, s)
        await session.commit()

        # only `a` still fires -> `b` must be resolved
        resolved = await resolve_stale(session, _CO, [a.id])
        await session.commit()
        assert resolved == 1

        by_id = {s.id: s for s in await list_situations(session, _CO)}
        assert by_id[a.id].status != "resolved"
        assert by_id[b.id].status == "resolved"
        assert by_id[b.id].resolved_at is not None


async def test_a_resolved_situation_reopens_if_it_fires_again() -> None:
    c = _sit("untriaged_issue:c")

    async with Session() as session:
        await save_situation(session, c)
        await resolve_stale(session, _CO, [])  # nothing fires -> c resolved
        await session.commit()
        assert {s.id: s.status for s in await list_situations(session, _CO)}[c.id] == "resolved"

        await save_situation(session, c)  # fires again
        await session.commit()
        again = {s.id: s for s in await list_situations(session, _CO)}[c.id]
        assert again.status == "open"
        assert again.resolved_at is None


async def test_a_situation_is_only_delivered_when_the_mail_really_went_out() -> None:
    """A dry run or a failed SMTP send must not claim the on-call was told."""
    from datetime import datetime as _dt

    from packages.core.situations import record_delivery
    from packages.shared.schema import DeliveryReceipt

    s = _sit("untriaged_issue:mail")
    async with Session() as session:
        await save_situation(session, s)
        await session.commit()

        for status in ("dry_run", "email_not_configured", "email_failed: boom"):
            await record_delivery(session, DeliveryReceipt(
                situation_id=s.id, channel="email", recipient="pm@x.com",
                status=status, delivered_at=datetime(2026, 7, 10, tzinfo=UTC)))
            await session.commit()
            got = {x.id: x for x in await list_situations(session, _CO)}[s.id]
            assert got.status != "delivered", f"{status!r} must not read as delivered"

        await record_delivery(session, DeliveryReceipt(
            situation_id=s.id, channel="email", recipient="pm@x.com",
            status="sent", delivered_at=_dt(2026, 7, 10, tzinfo=UTC)))
        await session.commit()
        got = {x.id: x for x in await list_situations(session, _CO)}[s.id]
        assert got.status == "delivered"


async def test_empty_active_set_resolves_everything() -> None:
    async with Session() as session:
        await save_situation(session, _sit("untriaged_issue:d"))
        await session.commit()
        await resolve_stale(session, _CO, [])
        await session.commit()
        assert all(s.status == "resolved" for s in await list_situations(session, _CO))
