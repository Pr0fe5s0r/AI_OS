from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from packages.core.db import Session
from packages.core.embeddings import embed
from packages.core.resolve import resolve
from packages.core.store import insert_event
from packages.shared.schema import Actor, Event
from verticals.software.config import ENTITY_RULES

_CO = "test-resolve"


def _ev(eid: str, source: str, type_: str, content: str) -> Event:
    return Event(
        id=eid,
        company_id=_CO,
        source=source,
        type=type_,
        actor=Actor(id=f"u_{eid}", name=f"User {eid}"),
        timestamp=datetime(2026, 7, 9, 12, 0, tzinfo=UTC),
        content=content,
        metadata={},
    )


async def test_resolve_links_two_events_sharing_an_id() -> None:
    # Two events across sources that share an explicit ID token "#4830".
    a = _ev("res-a", "github", "issue", "Checkout 500 tracked in PR #4830 at payment.")
    b = _ev("res-b", "zendesk", "ticket", "Agent note: linked to PR #4830, customer charged.")

    async with Session() as session:
        for ev in (a, b):
            vector = await asyncio.to_thread(embed, ev.content)
            await insert_event(session, ev, vector)
        await session.commit()

        await resolve(session, a, ENTITY_RULES)  # creates node A
        links = await resolve(session, b, ENTITY_RULES)  # links B -> A
        await session.commit()

    same_as = [x for x in links if x.edge_type == "SAME_AS" and x.target_node_id == "res-a"]
    assert same_as, f"expected a SAME_AS link to res-a, got {links}"
    assert same_as[0].method == "id"
    assert same_as[0].confidence == 1.0
