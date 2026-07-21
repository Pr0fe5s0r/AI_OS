from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from packages.core import graph
from packages.core.db import Session
from packages.core.embeddings import embed
from packages.core.resolve import resolve, thing_type_for
from packages.core.store import store_event
from packages.shared.schema import Actor, Event
from tests.conftest import INVENTORY_PROFILE, SOFTWARE_PROFILE

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


async def _seed(session, ev: Event) -> None:
    vector = await asyncio.to_thread(embed, ev.content)
    await store_event(session, ev)
    await graph.mirror_event(ev.company_id, ev.id, ev.timestamp, ev.source, vector)


async def test_resolve_links_two_events_sharing_an_id() -> None:
    await graph.bootstrap()
    await graph.wipe_company(_CO)

    # Two events across sources that share an explicit ID token "#4830".
    a = _ev("res-a", "github", "issue", "Checkout 500 tracked in PR #4830 at payment.")
    b = _ev("res-b", "zendesk", "ticket", "Agent note: linked to PR #4830, customer charged.")

    async with Session() as session:
        for ev in (a, b):
            await _seed(session, ev)
        await session.commit()

        await resolve(session, a, SOFTWARE_PROFILE.things, SOFTWARE_PROFILE.links)
        links = await resolve(session, b, SOFTWARE_PROFILE.things, SOFTWARE_PROFILE.links)
        await session.commit()

    same_as = [x for x in links if x.edge_type == "SAME_AS" and x.target_node_id == "res-a"]
    assert same_as, f"expected a SAME_AS link to res-a, got {links}"
    assert same_as[0].method == "id"
    assert same_as[0].confidence == 1.0


def test_thing_types_come_from_the_profile_not_the_engine() -> None:
    """The same engine call yields different thing types under different profiles."""
    assert thing_type_for("github", "issue", SOFTWARE_PROFILE.things) == "Incident"
    assert thing_type_for("github", "pull_request", SOFTWARE_PROFILE.things) == "PullRequest"
    assert thing_type_for("ops", "purchase_order", INVENTORY_PROFILE.things) == "PurchaseOrder"
    assert thing_type_for("ops", "delivery", INVENTORY_PROFILE.things) == "Delivery"
    # unknown combos fall back to the profile default, never a hardcoded name
    assert thing_type_for("x", "y", SOFTWARE_PROFILE.things) == "Event"
