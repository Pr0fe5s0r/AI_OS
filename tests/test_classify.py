from __future__ import annotations

import pytest
from sqlalchemy import text

from packages.core import classify
from packages.core.classify import (
    FALLBACK,
    Assignment,
    bulk_override,
    classes_for,
    create_class,
    delete_class,
    list_classes,
    needs_review,
    override,
)
from packages.core.store import list_items, put_item
from packages.shared.schema import Item, Scope, SourceRef
from tests.conftest import SCOPE, WORKSPACE

pytestmark = pytest.mark.needs_db


def doc(body: str, locator: str = "doc-1", title: str = "A document") -> Item:
    return Item(
        id="",
        scope=SCOPE,
        title=title,
        body=body,
        source=SourceRef(source="upload", locator=locator),
    )


async def _clean_classes(session):
    await session.execute(
        text("DELETE FROM kb_item_classes WHERE workspace_id = :t"), {"t": WORKSPACE}
    )
    await session.execute(text("DELETE FROM kb_classes WHERE workspace_id = :t"), {"t": WORKSPACE})
    await session.execute(
        text("DELETE FROM audit_log WHERE company_id = :t"), {"t": WORKSPACE}
    )
    await session.commit()


# ------------------------------- the taxonomy -------------------------------


async def test_the_platform_set_is_available_to_every_tenant(db):
    await _clean_classes(db)
    classes = await list_classes(db, SCOPE)
    ids = {c["class_id"] for c in classes}
    assert FALLBACK in ids
    assert {"report", "research", "strategy"} <= ids
    assert all(c["scope"] == "platform" for c in classes if c["class_id"] == FALLBACK)


async def test_a_tenant_can_add_a_class_without_a_deployment(db):
    """KB-2.0: adding a classification must not require a code change."""
    await _clean_classes(db)
    await create_class(db, SCOPE, "retainer", "Retainer", description="Contracted work")
    await db.commit()

    mine = {c["class_id"]: c for c in await list_classes(db, SCOPE)}
    assert mine["retainer"]["name"] == "Retainer"
    assert mine["retainer"]["scope"] == "workspace"


async def test_classes_support_hierarchy(db):
    await _clean_classes(db)
    await create_class(db, SCOPE, "paid-social", "Paid social", parent_id="performance")
    await db.commit()

    child = next(c for c in await list_classes(db, SCOPE) if c["class_id"] == "paid-social")
    assert child["parent_id"] == "performance"


async def test_system_classes_cannot_be_deleted(db):
    """They ship with the product and may be extended, never removed."""
    await _clean_classes(db)
    assert await delete_class(db, SCOPE, FALLBACK) is False
    assert FALLBACK in {c["class_id"] for c in await list_classes(db, SCOPE)}


async def test_another_tenants_class_is_invisible(db):
    await _clean_classes(db)
    await create_class(db, SCOPE, "private-cat", "Private category")
    await db.commit()

    other = Scope(workspace_id="unrelated-agency")
    assert "private-cat" not in {c["class_id"] for c in await list_classes(db, other)}


# ------------------------------ the decision ------------------------------


async def test_low_confidence_lands_in_the_fallback_never_nowhere(db, monkeypatch):
    """KB-2.a: where the KB cannot classify confidently, the item is routed to
    a defined fallback and flagged — never silently misfiled or dropped."""
    await _clean_classes(db)
    monkeypatch.setattr(
        classify, "_decide", lambda classes, item, suggested: [
            Assignment(FALLBACK, 0.0, "nothing matched")
        ]
    )
    stored = await put_item(db, doc("ambiguous content"))
    await db.commit()

    result = await classify.classify_item(db, SCOPE, stored.item)
    await db.commit()

    assert [a.class_id for a in result] == [FALLBACK]
    assert result[0].needs_review is True

    queued = await needs_review(db, SCOPE)
    assert stored.item.id in {q["item_id"] for q in queued}


async def test_an_invented_class_is_discarded_not_created(db, monkeypatch):
    """A model naming a class that does not exist must not silently create it."""
    await _clean_classes(db)
    monkeypatch.setattr(
        classify,
        "chat",
        lambda *a, **k: '{"classes":[{"class_id":"invented","confidence":0.9,"basis":"x"}]}',
    )
    stored = await put_item(db, doc("some content"))
    await db.commit()

    result = await classify.classify_item(db, SCOPE, stored.item)
    await db.commit()

    assert [a.class_id for a in result] == [FALLBACK]
    known = {c["class_id"] for c in await list_classes(db, SCOPE)}
    assert "invented" not in known


async def test_an_item_can_hold_several_classes(db, monkeypatch):
    await _clean_classes(db)
    monkeypatch.setattr(
        classify, "_decide", lambda classes, item, suggested: [
            Assignment("report", 0.9, "reads as a deliverable"),
            Assignment("performance", 0.8, "contains campaign metrics"),
        ]
    )
    stored = await put_item(db, doc("Q2 results"))
    await db.commit()
    await classify.classify_item(db, SCOPE, stored.item)
    await db.commit()

    tagged = await classes_for(db, SCOPE, [stored.item.id])
    assert {c["class_id"] for c in tagged[stored.item.id]} == {"report", "performance"}


# ------------------------------ human override ------------------------------


async def test_an_override_survives_reclassification(db, monkeypatch):
    """KB-2.b, the rule that outranks the rest: a human decision is sticky.

    Someone who corrects a filing must not find it reverted by the next sync.
    """
    await _clean_classes(db)
    monkeypatch.setattr(
        classify, "_decide", lambda classes, item, suggested: [
            Assignment("research", 0.9, "the model's view")
        ]
    )
    stored = await put_item(db, doc("content"))
    await db.commit()
    await classify.classify_item(db, SCOPE, stored.item)
    await db.commit()

    await override(db, SCOPE, stored.item.id, ["strategy"], actor="kim@agency.com")
    await db.commit()

    # Re-classification runs again — and must change nothing.
    result = await classify.classify_item(db, SCOPE, stored.item)
    await db.commit()

    assert [a.class_id for a in result] == ["strategy"]
    tagged = await classes_for(db, SCOPE, [stored.item.id])
    assert {c["class_id"] for c in tagged[stored.item.id]} == {"strategy"}
    assert tagged[stored.item.id][0]["pinned"] is True


async def test_an_override_is_audited_with_before_and_after(db, monkeypatch):
    await _clean_classes(db)
    monkeypatch.setattr(
        classify, "_decide", lambda classes, item, suggested: [
            Assignment("research", 0.9, "auto")
        ]
    )
    stored = await put_item(db, doc("content"))
    await db.commit()
    await classify.classify_item(db, SCOPE, stored.item)
    await db.commit()

    await override(db, SCOPE, stored.item.id, ["client"], actor="kim@agency.com")
    await db.commit()

    row = (
        await db.execute(
            text(
                "SELECT actor, action, target, metadata FROM audit_log "
                "WHERE company_id = :t AND action = 'classification.override'"
            ),
            {"t": WORKSPACE},
        )
    ).one()
    assert row.actor == "kim@agency.com"
    assert row.target == stored.item.id
    assert row.metadata["from"] == ["research"]
    assert row.metadata["to"] == ["client"]


async def test_bulk_override_files_a_whole_set(db):
    await _clean_classes(db)
    ids = []
    for n in range(3):
        stored = await put_item(db, doc(f"content {n}", locator=f"doc-{n}"))
        ids.append(stored.item.id)
    await db.commit()

    count = await bulk_override(db, SCOPE, ids, ["client"], actor="kim@agency.com")
    await db.commit()

    assert count == 3
    tagged = await classes_for(db, SCOPE, ids)
    assert all(t[0]["class_id"] == "client" and t[0]["pinned"] for t in tagged.values())


async def test_the_catalogue_can_be_filtered_by_class(db):
    await _clean_classes(db)
    a = await put_item(db, doc("filed content", locator="a"))
    await put_item(db, doc("unfiled content", locator="b"))
    await db.commit()
    await override(db, SCOPE, a.item.id, ["report"], actor="kim@agency.com")
    await db.commit()

    filed = await list_items(db, SCOPE, class_id="report")
    assert [i.id for i in filed] == [a.item.id]
    assert len(await list_items(db, SCOPE)) == 2


async def test_an_item_with_several_classes_appears_once(db):
    """A join would return the item once per class."""
    await _clean_classes(db)
    stored = await put_item(db, doc("content"))
    await db.commit()
    await override(db, SCOPE, stored.item.id, ["report", "performance", "client"], "kim@a.com")
    await db.commit()

    assert len(await list_items(db, SCOPE, class_id="report")) == 1
    assert len(await list_items(db, SCOPE)) == 1
