from __future__ import annotations

from sqlalchemy import text

from packages.core.store import (
    get_item,
    item_versions,
    list_items,
    mark_failed,
    put_item,
)
from packages.shared.schema import Item, Lifecycle, SourceRef
from tests.conftest import BRAND_A, BRAND_B, OTHER, SCOPE

# The write path has exactly three outcomes. These tests pin all three, plus
# the integrity rule that makes "is this current?" answerable.


def item(body: str, locator: str = "doc-1", scope=SCOPE, title: str = "A document") -> Item:
    return Item(
        id="",
        scope=scope,
        title=title,
        body=body,
        source=SourceRef(source="upload", locator=locator),
    )


async def test_first_write_creates_version_one(db):
    result = await put_item(db, item("hello world"))
    await db.commit()

    assert result.outcome == "created"
    assert result.item.version == 1
    assert result.embedded_needed is True
    assert result.item.status == Lifecycle.ACTIVE


async def test_identical_content_is_skipped_without_embedding(db):
    """The cost control in KB-8 is this: unchanged content never reaches a
    model. A drive that has not moved must be free to re-sync."""
    await put_item(db, item("unchanged text"))
    await db.commit()

    second = await put_item(db, item("unchanged text"))
    await db.commit()

    assert second.outcome == "unchanged"
    assert second.embedded_needed is False
    assert second.changed is False


async def test_changed_content_supersedes_the_previous_version(db):
    first = await put_item(db, item("original text"))
    await db.commit()
    second = await put_item(db, item("revised text"))
    await db.commit()

    assert second.outcome == "versioned"
    assert second.item.version == 2
    # Same logical item, not a second one.
    assert second.item.id == first.item.id
    assert second.item.supersedes == f"{first.item.id}@1"

    prior = await get_item(db, SCOPE, first.item.id, version=1)
    assert prior is not None and prior.status == Lifecycle.SUPERSEDED


async def test_only_one_version_is_ever_current(db):
    """Enforced by a partial unique index, not by application care — so a
    concurrent writer cannot produce two answers to "which is current?"."""
    await put_item(db, item("v1"))
    await db.commit()
    await put_item(db, item("v2"))
    await db.commit()
    await put_item(db, item("v3"))
    await db.commit()

    active = (
        await db.execute(
            text(
                "SELECT count(*) AS n FROM kb_items "
                "WHERE tenant_id = :t AND status = 'active'"
            ),
            {"t": SCOPE.tenant_id},
        )
    ).scalar_one()
    assert active == 1

    current = await get_item(db, SCOPE, (await put_item(db, item("v3"))).item.id)
    assert current is not None and current.body == "v3"


async def test_lineage_is_traceable(db):
    """KB-1 requires the prior version retained with traceable lineage."""
    await put_item(db, item("draft"))
    await db.commit()
    latest = await put_item(db, item("final"))
    await db.commit()

    history = await item_versions(db, SCOPE, latest.item.id)
    assert [h.version for h in history] == [2, 1]
    assert [h.body for h in history] == ["final", "draft"]


async def test_a_point_in_time_read_returns_the_old_version(db):
    created = await put_item(db, item("as it was"))
    await db.commit()
    await put_item(db, item("as it is now"))
    await db.commit()

    old = await get_item(db, SCOPE, created.item.id, version=1)
    assert old is not None and old.body == "as it was"


async def test_another_agency_cannot_read_this_one(db):
    """Cross-tenant leakage is a critical defect, so it is asserted directly."""
    created = await put_item(db, item("confidential"))
    await db.commit()

    assert await get_item(db, OTHER, created.item.id) is None
    assert await list_items(db, OTHER) == []


async def test_brands_are_isolated_from_each_other(db):
    """The second level of tenancy: two client brands under one agency."""
    a = await put_item(db, item("brand a content", scope=BRAND_A))
    await db.commit()
    await put_item(db, item("brand b content", scope=BRAND_B))
    await db.commit()

    assert await get_item(db, BRAND_B, a.item.id) is None

    only_a = await list_items(db, BRAND_A)
    assert [i.body for i in only_a] == ["brand a content"]

    # The agency itself sees across its own brands.
    everything = await list_items(db, SCOPE)
    assert len(everything) == 2


async def test_the_same_file_in_two_brands_stays_two_items(db):
    await put_item(db, item("shared doc", locator="same.pdf", scope=BRAND_A))
    await db.commit()
    await put_item(db, item("shared doc", locator="same.pdf", scope=BRAND_B))
    await db.commit()

    assert len(await list_items(db, SCOPE)) == 2


async def test_listing_filters_by_source(db):
    await put_item(db, item("from drive", locator="d1"))
    await db.commit()
    notion = Item(
        id="",
        scope=SCOPE,
        title="From Notion",
        body="notion content",
        source=SourceRef(source="notion", locator="n1"),
    )
    await put_item(db, notion)
    await db.commit()

    assert len(await list_items(db, SCOPE, source="notion")) == 1
    assert len(await list_items(db, SCOPE)) == 2


async def test_a_failure_is_recorded_not_swallowed(db):
    """Ingestion failures must be visible and re-runnable (KB-1/KB-3)."""
    created = await put_item(db, item("partial"))
    await db.commit()

    await mark_failed(db, SCOPE, created.item.id, created.item.version, "no text layer")
    await db.commit()

    assert await get_item(db, SCOPE, created.item.id) is None  # no longer active
    failed = await list_items(db, SCOPE, status=Lifecycle.FAILED)
    assert len(failed) == 1
    assert failed[0].metadata["failure"] == "no text layer"
