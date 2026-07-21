from __future__ import annotations

from sqlalchemy import text

from apps.common.analysis import extract_target
from packages.core.db import Session
from packages.core.tickets import close_ticket, create_ticket, list_tickets, ticket_for_situation
from packages.core.tokens import consume_token, issue_token, peek_token
from packages.shared.schema import Ticket
from tests.conftest import SOFTWARE_PROFILE

_CO = "test-tokens"
_PAYLOAD = {"situation_id": "needs_owner:gh-1", "assignee": "sam",
            "target": {"repo": "a/b", "number": "7"}}


# ------------------------------- email tokens -------------------------------


async def test_token_round_trips_its_payload() -> None:
    async with Session() as session:
        token = await issue_token(session, _CO, "assign", _PAYLOAD)
        await session.commit()
        body = await peek_token(session, token, "assign")
    assert body is not None
    assert body["assignee"] == "sam" and body["target"]["repo"] == "a/b"


async def test_peek_does_not_consume_so_mail_scanners_cannot_fire_it() -> None:
    """Gmail/Outlook pre-fetch every link. A GET must leave the token usable."""
    async with Session() as session:
        token = await issue_token(session, _CO, "assign", _PAYLOAD)
        await session.commit()

        assert await peek_token(session, token, "assign") is not None
        assert await peek_token(session, token, "assign") is not None  # still valid
        assert await consume_token(session, token, "assign") is not None  # the POST works
        await session.commit()


async def test_a_token_cannot_be_replayed() -> None:
    async with Session() as session:
        token = await issue_token(session, _CO, "assign", _PAYLOAD)
        await session.commit()
        assert await consume_token(session, token, "assign") is not None
        await session.commit()
        # second click does nothing
        assert await consume_token(session, token, "assign") is None
        assert await peek_token(session, token, "assign") is None


async def test_a_token_is_bound_to_its_purpose() -> None:
    async with Session() as session:
        token = await issue_token(session, _CO, "assign", _PAYLOAD)
        await session.commit()
        assert await peek_token(session, token, "close") is None


async def test_a_forged_token_is_rejected() -> None:
    async with Session() as session:
        assert await peek_token(session, "not-a-real-token", "assign") is None
        assert await consume_token(session, "not-a-real-token", "assign") is None


# --------------------------------- tickets ---------------------------------


async def test_ticket_lifecycle_open_then_done() -> None:
    ticket = Ticket(
        company_id=_CO, situation_id="needs_owner:gh-9", title="Fix the API",
        description="It is down.", assignee="sam",
        external_url="https://github.com/a/b/issues/9",
    )
    async with Session() as session:
        # re-runnable: drop anything a previous run left behind
        await session.execute(text("DELETE FROM tickets WHERE company_id = :c"), {"c": _CO})
        await session.commit()

        ticket_id = await create_ticket(session, ticket)
        await session.commit()

        found = await ticket_for_situation(session, _CO, "needs_owner:gh-9")
        assert found and found.id == ticket_id and found.status == "open"

        closed = await close_ticket(session, _CO, ticket_id)
        await session.commit()
        assert closed and closed.status == "done" and closed.closed_at is not None

        mine = await list_tickets(session, _CO, assignee="sam")
        assert any(t.id == ticket_id for t in mine)
        assert not await list_tickets(session, _CO, assignee="nobody")


def test_the_action_target_is_recoverable_from_the_ticket_url() -> None:
    """The profile's target_url_pattern, not the engine, knows the URL shape."""
    assert extract_target(SOFTWARE_PROFILE, "https://github.com/karthikeyan846/Chatbot/issues/4") == {
        "repo": "karthikeyan846/Chatbot", "number": "4",
    }
    assert extract_target(SOFTWARE_PROFILE, None) is None
    assert extract_target(SOFTWARE_PROFILE, "https://example.com/nope") is None
