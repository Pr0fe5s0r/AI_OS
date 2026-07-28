from __future__ import annotations

import pytest
from sqlalchemy import text

from packages.core import search as search_mod
from packages.core.search import RetrievalConfig, search_traced
from packages.core.store import put_item
from packages.core.tracing import get_trace, list_traces, record, stats
from packages.shared.schema import Item, Scope, SourceRef
from tests.conftest import SCOPE, WORKSPACE

pytestmark = pytest.mark.needs_db

# Retrieval that cannot explain itself is retrieval nobody can debug. These
# pin the explanation, not just the answer.


def doc(body: str, locator: str, title: str = "Doc") -> Item:
    return Item(
        id="",
        scope=SCOPE,
        title=title,
        body=body,
        source=SourceRef(source="upload", locator=locator),
    )


@pytest.fixture(autouse=True)
async def clean_traces(db):
    await db.execute(
        text("DELETE FROM query_traces WHERE workspace_id = :ws"), {"ws": WORKSPACE}
    )
    await db.commit()


async def test_a_trace_records_both_arms_and_what_came_back(db, monkeypatch):
    # No embedding service in the test environment, so the semantic arm is
    # deliberately absent — which is itself a case worth pinning.
    await put_item(db, doc("paid conversions fell in the second quarter", "q2.md"))
    await db.commit()

    hits, trace = await search_traced(db, SCOPE, "paid conversions")

    assert trace.trace_id
    assert trace.query == "paid conversions"
    assert [h.item_id for h in hits] == trace.returned
    # The keyword arm found it, and the trace says with what rank.
    assert trace.keyword and trace.keyword[0]["rank"] > 0
    # Fusion is shown per candidate, so a surprising ranking can be read back.
    assert trace.fused and "score" in trace.fused[0]
    assert trace.timings_ms["total"] >= 0


async def test_the_config_that_actually_ran_is_recorded(db):
    """Retrieval behaviour changing is the usual cause of output quality
    changing, so the trace keeps the settings the call resolved to."""
    await put_item(db, doc("some content about pricing", "p.md"))
    await db.commit()

    cfg = RetrievalConfig(limit=3, semantic_weight=0.9, min_score=0.0)
    _, trace = await search_traced(db, SCOPE, "pricing", cfg)

    assert trace.config["limit"] == 3
    assert trace.config["semantic_weight"] == 0.9


async def test_a_degraded_arm_is_named_not_hidden(db, monkeypatch):
    """An answer produced without the semantic arm must not look identical to
    a healthy one."""
    def explode(*a, **k):
        raise RuntimeError("vector store down")

    monkeypatch.setattr(search_mod, "embed", explode)
    await put_item(db, doc("keyword reachable content", "k.md"))
    await db.commit()

    hits, trace = await search_traced(db, SCOPE, "keyword reachable")

    assert trace.degraded is not None
    assert "semantic" in trace.degraded
    # Still answered, from the keyword arm alone.
    assert len(hits) == 1


async def test_a_query_that_finds_nothing_still_leaves_a_trace(db):
    """The empty ones are the interesting ones — without a trace, 'it returned
    nothing' has no explanation attached to it."""
    _, trace = await search_traced(db, SCOPE, "nothing whatsoever matches this")
    assert trace.returned == []

    await record(db, SCOPE, trace)
    await db.commit()

    empty = await list_traces(db, SCOPE, only_empty=True)
    assert [t["trace_id"] for t in empty] == [trace.trace_id]


async def test_a_trace_survives_a_round_trip_through_storage(db):
    await put_item(db, doc("quarterly revenue analysis", "rev.md"))
    await db.commit()

    _, trace = await search_traced(db, SCOPE, "revenue")
    await record(db, SCOPE, trace, via="api_key", actor="key:sdk")
    await db.commit()

    stored = await get_trace(db, SCOPE, trace.trace_id)
    assert stored is not None
    assert stored["query"] == "revenue"
    assert stored["via"] == "api_key"
    assert stored["returned"] == trace.returned
    assert stored["fused"][0]["item_id"] == trace.fused[0]["item_id"]


async def test_traces_are_scoped_to_their_workspace(db):
    _, trace = await search_traced(db, SCOPE, "anything")
    await record(db, SCOPE, trace)
    await db.commit()

    other = Scope(workspace_id="unrelated-workspace")
    assert await get_trace(db, other, trace.trace_id) is None
    assert await list_traces(db, other) == []


async def test_recording_never_breaks_the_answer(db):
    """Observability that can break the thing it observes is worse than none."""
    _, trace = await search_traced(db, SCOPE, "x")
    trace.timings_ms = {"total": object()}  # type: ignore[dict-item]  unserialisable

    # Must not raise, even though the payload cannot be encoded.
    assert await record(db, SCOPE, trace) == trace.trace_id


async def test_content_about_the_future_cannot_outrank_everything(db):
    """A period that has not finished yet gives a negative age, and an
    unclamped exp(-age) grows without bound — a plan for next year scored 2.14
    and outranked results with far higher similarity."""
    from datetime import UTC, datetime, timedelta

    ahead = datetime.now(UTC) + timedelta(days=180)
    plan = doc("annual media plan and budget split", "plan.md", title="Media plan")
    await put_item(db, plan.model_copy(update={"period_end": ahead}))
    await put_item(db, doc("annual media plan retrospective", "past.md"))
    await db.commit()

    _, trace = await search_traced(db, SCOPE, "annual media plan")

    assert trace.fused, "expected candidates"
    for candidate in trace.fused:
        assert candidate["recency"] <= 1.0, candidate
        assert candidate["score"] <= 1.0, candidate


async def test_stats_summarise_the_window(db):
    await put_item(db, doc("content for stats", "s.md"))
    await db.commit()

    for q in ("content", "content for", "no match at all here"):
        _, trace = await search_traced(db, SCOPE, q)
        await record(db, SCOPE, trace)
    await db.commit()

    summary = await stats(db, SCOPE, hours=1)
    assert summary["queries"] == 3
    assert summary["empty"] == 1
    assert summary["avg_ms"] >= 0
