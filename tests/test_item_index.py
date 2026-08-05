from __future__ import annotations

from sqlalchemy import text

from packages.core.store import (
    edit_item,
    get_item,
    item_versions,
    list_items,
    mark_failed,
    put_item,
    record_failure,
)
from packages.shared.schema import Item, Lifecycle, SourceRef
from tests.conftest import COLL_A, COLL_B, OTHER, SCOPE

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
                "WHERE workspace_id = :t AND status = 'active'"
            ),
            {"t": SCOPE.workspace_id},
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


async def test_editing_title_or_body_creates_a_searchable_new_version(db):
    created = await put_item(db, item("old body", title="Old title"))
    await db.commit()

    edited, needs_embedding = await edit_item(
        db, SCOPE, created.item.id, title="New title", body="new body"
    )
    await db.commit()

    assert edited.id == created.item.id
    assert edited.version == 2
    assert edited.title == "New title"
    assert edited.body == "new body"
    assert needs_embedding is True
    history = await item_versions(db, SCOPE, edited.id)
    assert [entry.version for entry in history] == [2, 1]


async def test_metadata_edit_merges_without_creating_a_content_version(db):
    created = await put_item(db, item("body"))
    await db.commit()

    edited, needs_embedding = await edit_item(
        db, SCOPE, created.item.id, metadata={"owner": "operations"}
    )
    await db.commit()

    assert edited.version == 1
    assert edited.metadata["owner"] == "operations"
    assert needs_embedding is False
    assert len(await item_versions(db, SCOPE, edited.id)) == 1


async def test_a_point_in_time_read_returns_the_old_version(db):
    created = await put_item(db, item("as it was"))
    await db.commit()
    await put_item(db, item("as it is now"))
    await db.commit()

    old = await get_item(db, SCOPE, created.item.id, version=1)
    assert old is not None and old.body == "as it was"


async def test_another_agency_cannot_read_this_one(db):
    """Cross-workspace leakage is a critical defect, so it is asserted directly."""
    created = await put_item(db, item("confidential"))
    await db.commit()

    assert await get_item(db, OTHER, created.item.id) is None
    assert await list_items(db, OTHER) == []


async def test_collections_are_isolated_from_each_other(db):
    """The second level of tenancy: two client brands under one agency."""
    a = await put_item(db, item("collection a content", scope=COLL_A))
    await db.commit()
    await put_item(db, item("collection b content", scope=COLL_B))
    await db.commit()

    assert await get_item(db, COLL_B, a.item.id) is None

    only_a = await list_items(db, COLL_A)
    assert [i.body for i in only_a] == ["collection a content"]

    # The agency itself sees across its own brands.
    everything = await list_items(db, SCOPE)
    assert len(everything) == 2


async def test_the_same_file_in_two_collections_stays_two_items(db):
    await put_item(db, item("shared doc", locator="same.pdf", scope=COLL_A))
    await db.commit()
    await put_item(db, item("shared doc", locator="same.pdf", scope=COLL_B))
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


async def test_a_first_failure_leaves_a_row_to_look_at(db):
    """A file that never parsed had no row at all, so the only record of why
    was the job's return value — which nothing reads."""
    source = SourceRef(source="upload", locator="broken.docx")
    await record_failure(db, SCOPE, source, "broken.docx", "not a readable Word document")
    await db.commit()

    failed = await list_items(db, SCOPE, status=Lifecycle.FAILED)
    assert len(failed) == 1
    assert failed[0].metadata["failure"] == "not a readable Word document"
    assert failed[0].body == ""  # nothing was extracted, so nothing is claimed


async def test_a_bad_re_upload_does_not_delete_a_good_document(db):
    """A document that indexed cleanly must not vanish from search because
    someone re-uploaded a corrupt copy of it."""
    good = await put_item(db, item("real content", locator="report.pdf"))
    await db.commit()

    await record_failure(
        db, SCOPE, SourceRef(source="upload", locator="report.pdf"), "report.pdf", "no text layer"
    )
    await db.commit()

    still_there = await get_item(db, SCOPE, good.item.id)
    assert still_there is not None
    assert still_there.body == "real content"
    assert still_there.metadata["failure"] == "no text layer"  # said, not silent
    assert await list_items(db, SCOPE, status=Lifecycle.FAILED) == []
