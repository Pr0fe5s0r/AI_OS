from __future__ import annotations

import pytest
from sqlalchemy import text

from packages.core import consolidate
from packages.core.consolidate import (
    MAX_MERGE_GROUP,
    find_groups,
    summary_id,
    text_hash,
)
from packages.core.store import put_item
from packages.shared.schema import Item, SourceRef
from tests.conftest import SCOPE

pytestmark = pytest.mark.needs_db

# Consolidation rewrites the retrievable surface: it replaces passages with
# model-written summaries and decays archived nodes away. Every test here
# guards something that would be silent and irreversible if it broke.


def _vec(*values: float) -> list[float]:
    return list(values)


def _point(node_id: str, vector: list[float]) -> dict:
    return {"id": node_id, "embedding": vector}


# --------------------------------- grouping ---------------------------------


def test_equivalent_nodes_group_together():
    points = [
        _point("a", _vec(1.0, 0.0, 0.0)),
        _point("b", _vec(0.99, 0.01, 0.0)),
        _point("far", _vec(0.0, 1.0, 0.0)),
    ]
    groups = find_groups(points, threshold=0.9)
    assert len(groups) == 1
    assert set(groups[0]) == {"a", "b"}


def test_grouping_is_transitive():
    """If A matches B and B matches C, all three are one statement. Merging
    pairwise would build a summary of a summary and lose provenance a level at
    a time."""
    points = [
        _point("a", _vec(1.0, 0.00, 0.0)),
        _point("b", _vec(0.99, 0.10, 0.0)),
        _point("c", _vec(0.97, 0.20, 0.0)),
    ]
    groups = find_groups(points, threshold=0.97)
    assert len(groups) == 1 and len(groups[0]) == 3


def test_a_runaway_group_is_left_alone():
    """Fifty near-identical vectors means the threshold is wrong for this
    collection, not that fifty passages say the same thing. Merging them would
    destroy the collection in one pass."""
    points = [_point(f"n{i}", _vec(1.0, i * 0.0001, 0.0)) for i in range(MAX_MERGE_GROUP + 5)]
    assert find_groups(points, threshold=0.9) == []


def test_unrelated_nodes_are_never_grouped():
    points = [
        _point("a", _vec(1.0, 0.0, 0.0)),
        _point("b", _vec(0.0, 1.0, 0.0)),
        _point("c", _vec(0.0, 0.0, 1.0)),
    ]
    assert find_groups(points, threshold=0.88) == []


def test_a_summary_id_is_stable_for_the_same_members():
    """Otherwise the same group merged twice becomes two summaries, and the
    store grows a pile of near-identical nodes it can never reconcile."""
    assert summary_id(["b", "a"]) == summary_id(["a", "b"])
    assert summary_id(["a", "b"]) != summary_id(["a", "c"])


def test_duplicate_detection_ignores_whitespace():
    assert text_hash("one  two\nthree") == text_hash("one two three")
    assert text_hash("one two") != text_hash("one three")


# ------------------------------ against the db ------------------------------


def _doc(body: str, locator: str) -> Item:
    return Item(
        id="",
        scope=SCOPE,
        title="Doc",
        body=body,
        source=SourceRef(source="upload", locator=locator),
    )


async def test_exact_duplicates_collapse_to_one(db):
    body = "# Notice\n\n" + ("The retention period is ninety days. " * 40)
    await put_item(db, _doc(body, "a.md"))
    await put_item(db, _doc(body, "b.md"))
    await db.commit()

    before = (
        await db.execute(
            text("SELECT count(*) FROM kb_chunks WHERE workspace_id = :w AND archived_at IS NULL"),
            {"w": SCOPE.workspace_id},
        )
    ).scalar_one()

    collapsed = await consolidate.dedupe(db, SCOPE)
    await db.commit()

    assert collapsed > 0
    after = (
        await db.execute(
            text("SELECT count(*) FROM kb_chunks WHERE workspace_id = :w AND archived_at IS NULL"),
            {"w": SCOPE.workspace_id},
        )
    ).scalar_one()
    assert after < before
    # Collapsed, not destroyed — the row is still there, archived.
    assert (
        await db.execute(
            text("SELECT count(*) FROM kb_chunks WHERE workspace_id = :w"),
            {"w": SCOPE.workspace_id},
        )
    ).scalar_one() >= before


async def test_an_archived_node_cannot_be_retrieved(db):
    """Archiving is the store deciding a node is superseded. Answering from it
    afterwards would contradict that decision."""
    await put_item(db, _doc("# X\n\n" + ("Unique retrievable sentence here. " * 40), "x.md"))
    await db.commit()

    hits = await consolidate.dedupe(db, SCOPE)  # nothing to collapse yet
    assert hits == 0

    await db.execute(
        text("UPDATE kb_chunks SET archived_at = now() WHERE workspace_id = :w"),
        {"w": SCOPE.workspace_id},
    )
    await db.commit()

    from packages.core.search import search_traced

    _, trace = await search_traced(db, SCOPE, "unique retrievable sentence")
    assert trace.keyword == []


async def test_decay_is_computed_from_the_clock_not_the_pass_count(db):
    """Recomputed from archived_at rather than multiplied down each run, so a
    pass that was missed — or one that ran twice — gives the same answer."""
    await put_item(db, _doc("# Y\n\n" + ("Decaying content here. " * 40), "y.md"))
    await db.commit()
    await db.execute(
        text(
            "UPDATE kb_chunks SET archived_at = now() - interval '48 hours' "
            "WHERE workspace_id = :w"
        ),
        {"w": SCOPE.workspace_id},
    )
    await db.commit()

    await consolidate.decay(db, SCOPE)
    await db.commit()
    once = (
        await db.execute(
            text("SELECT importance FROM kb_chunks WHERE workspace_id = :w LIMIT 1"),
            {"w": SCOPE.workspace_id},
        )
    ).scalar_one()

    await consolidate.decay(db, SCOPE)
    await db.commit()
    twice = (
        await db.execute(
            text("SELECT importance FROM kb_chunks WHERE workspace_id = :w LIMIT 1"),
            {"w": SCOPE.workspace_id},
        )
    ).scalar_one()

    assert once == pytest.approx(twice, abs=1e-4)
    # One half-life from a 0.5 start.
    assert once == pytest.approx(0.25, abs=0.02)


async def test_a_fully_decayed_node_is_dropped(db):
    await put_item(db, _doc("# Z\n\n" + ("Long dead content. " * 40), "z.md"))
    await db.commit()
    await db.execute(
        text(
            "UPDATE kb_chunks SET archived_at = now() - interval '30 days' "
            "WHERE workspace_id = :w"
        ),
        {"w": SCOPE.workspace_id},
    )
    await db.commit()

    _, dropped = await consolidate.decay(db, SCOPE)
    await db.commit()
    assert dropped > 0


async def test_retrieval_raises_importance(db):
    """What gets used survives; what is never used decays. Retrieval is the
    only evidence the store has about which passages matter."""
    result = await put_item(db, _doc("# W\n\n" + ("Frequently asked content. " * 40), "w.md"))
    await db.commit()

    ids = (
        await db.execute(
            text("SELECT chunk_id FROM kb_chunks WHERE workspace_id = :w AND item_id = :i"),
            {"w": SCOPE.workspace_id, "i": result.item.id},
        )
    ).scalars().all()

    await consolidate.touch(db, SCOPE, list(ids))
    await db.commit()

    row = (
        await db.execute(
            text(
                "SELECT importance, access_count FROM kb_chunks "
                "WHERE workspace_id = :w AND chunk_id = :c"
            ),
            {"w": SCOPE.workspace_id, "c": ids[0]},
        )
    ).one()
    assert row.access_count == 1
    assert row.importance > 0.5


async def test_importance_cannot_exceed_one(db):
    """A node must not become permanent by being asked for repeatedly."""
    result = await put_item(db, _doc("# V\n\n" + ("Very popular content. " * 40), "v.md"))
    await db.commit()
    ids = (
        await db.execute(
            text("SELECT chunk_id FROM kb_chunks WHERE workspace_id = :w AND item_id = :i"),
            {"w": SCOPE.workspace_id, "i": result.item.id},
        )
    ).scalars().all()

    for _ in range(60):
        await consolidate.touch(db, SCOPE, list(ids))
    await db.commit()

    top = (
        await db.execute(
            text("SELECT max(importance) FROM kb_chunks WHERE workspace_id = :w"),
            {"w": SCOPE.workspace_id},
        )
    ).scalar_one()
    assert top <= 1.0


async def test_a_pass_reports_why_nothing_merged(db):
    """"Nothing merged" is ambiguous between "the data is dissimilar" and "the
    threshold is too high". The closest pair it saw settles it — which is how
    we found that the reference implementation's 0.88 merges nothing at all on
    this embedding model."""
    await put_item(db, _doc("# A\n\n" + ("Completely distinct subject one. " * 40), "a.md"))
    await put_item(db, _doc("# B\n\n" + ("An entirely unrelated subject two. " * 40), "b.md"))
    await db.commit()

    outcome = await consolidate.run_once(db, SCOPE)
    assert outcome.merged == 0
    assert outcome.threshold > 0
    assert outcome.closest_pair >= 0.0
