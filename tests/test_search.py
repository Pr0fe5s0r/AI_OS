from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from packages.core import graph
from packages.core.db import Session
from packages.core.embeddings import embed
from packages.core.search import search
from packages.core.store import store_event
from packages.shared.schema import Actor, Event

_CO = "test-search"

_CHECKOUT = Event(
    id="test-checkout-500",
    company_id=_CO,
    source="github",
    type="issue",
    actor=Actor(id="u_test", name="Test Bot"),
    timestamp=datetime(2026, 7, 9, 12, 0, tzinfo=UTC),
    content=(
        "Checkout returns a 500 error at the payment step; the card is charged "
        "but the order is never marked paid."
    ),
    metadata={"labels": ["bug", "payments"]},
)

_UNRELATED = Event(
    id="test-darkmode",
    company_id=_CO,
    source="github",
    type="issue",
    actor=Actor(id="u_test", name="Test Bot"),
    timestamp=datetime(2026, 7, 9, 12, 0, tzinfo=UTC),
    content="Add dark mode support to the account settings page.",
    metadata={},
)


async def test_semantic_search_finds_checkout_event() -> None:
    await graph.bootstrap()
    await graph.wipe_company(_CO)

    async with Session() as session:
        for event in (_CHECKOUT, _UNRELATED):
            vector = await asyncio.to_thread(embed, event.content)
            await store_event(session, event)
            await graph.mirror_event(_CO, event.id, event.timestamp, event.source, vector)
        await session.commit()

    # "payment failure" shares no keywords with "checkout 500 error" — this only
    # works if the Neo4j vector index is really answering.
    async with Session() as session:
        results = await search(session, _CO, "payment failure")

    assert results, "expected search results"
    ids = [r["id"] for r in results]
    assert "test-checkout-500" in ids
    assert results[0]["id"] == "test-checkout-500"
