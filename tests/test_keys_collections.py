from __future__ import annotations

import pytest

from packages.core.collections import (
    create_cluster,
    create_collection,
    delete_collection,
    get_collection,
    list_clusters,
    slugify,
    valid_id,
)
from packages.core.keys import create_key, list_keys, resolve_key, revoke_key
from packages.core.store import put_item
from packages.shared.schema import Item, Scope, SourceRef
from tests.conftest import WORKSPACE

pytestmark = pytest.mark.needs_db


def doc(body: str, locator: str, scope: Scope) -> Item:
    return Item(
        id="",
        scope=scope,
        title=locator,
        body=body,
        source=SourceRef(source="upload", locator=locator),
    )


# --------------------------------- naming ---------------------------------


def test_ids_are_slugged_and_validated():
    assert slugify("Client Research 2026") == "client-research-2026"
    assert valid_id("client-research-2026")
    # An id ends up in URLs and SDK snippets, so it stays to a safe alphabet.
    assert not valid_id("Client Research")
    assert not valid_id("-leading-dash")


# ------------------------------- collections -------------------------------


async def test_a_collection_lands_in_a_cluster(db):
    await create_collection(db, WORKSPACE, "Client research")
    await db.commit()

    clusters = await list_clusters(db, WORKSPACE)
    assert len(clusters) == 1
    assert clusters[0]["cluster_id"] == "default"
    assert [c["collection_id"] for c in clusters[0]["collections"]] == ["client-research"]


async def test_collections_can_be_grouped_into_clusters(db):
    await create_cluster(db, WORKSPACE, "Production")
    await create_collection(db, WORKSPACE, "Live docs", cluster_id="production")
    await create_collection(db, WORKSPACE, "Scratch")
    await db.commit()

    by_id = {c["cluster_id"]: c for c in await list_clusters(db, WORKSPACE)}
    assert [c["collection_id"] for c in by_id["production"]["collections"]] == ["live-docs"]
    assert [c["collection_id"] for c in by_id["default"]["collections"]] == ["scratch"]


async def test_a_collection_cannot_be_made_in_a_cluster_that_does_not_exist(db):
    with pytest.raises(ValueError, match="No such cluster"):
        await create_collection(db, WORKSPACE, "Orphan", cluster_id="imaginary")


async def test_a_collection_records_its_embedding_model_and_dimensions(db):
    """Fixed at creation: changing either invalidates every vector inside it,
    which is a migration rather than a setting."""
    await create_collection(db, WORKSPACE, "Vectors")
    await db.commit()

    found = await get_collection(db, WORKSPACE, "vectors")
    assert found is not None
    assert found["dimensions"] > 0
    assert "embedding_model" in found


async def test_collection_stats_count_only_what_is_current(db):
    scope = Scope(workspace_id=WORKSPACE, collection_id="stats")
    await create_collection(db, WORKSPACE, "Stats")
    await put_item(db, doc("first version", "a.md", scope))
    await put_item(db, doc("second version", "a.md", scope))  # supersedes the first
    await put_item(db, doc("another document", "b.md", scope))
    await db.commit()

    found = await get_collection(db, WORKSPACE, "stats")
    assert found is not None
    assert found["stats"]["items"] == 2
    assert found["stats"]["superseded"] == 1


async def test_item_counts_are_per_collection(db):
    a = Scope(workspace_id=WORKSPACE, collection_id="alpha")
    b = Scope(workspace_id=WORKSPACE, collection_id="beta")
    await create_collection(db, WORKSPACE, "Alpha")
    await create_collection(db, WORKSPACE, "Beta")
    await put_item(db, doc("one", "1.md", a))
    await put_item(db, doc("two", "2.md", a))
    await put_item(db, doc("three", "3.md", b))
    await db.commit()

    counts = {
        c["collection_id"]: c["items"]
        for cluster in await list_clusters(db, WORKSPACE)
        for c in cluster["collections"]
    }
    assert counts == {"alpha": 2, "beta": 1}


async def test_dropping_a_collection_reports_what_it_removed(db):
    scope = Scope(workspace_id=WORKSPACE, collection_id="doomed")
    await create_collection(db, WORKSPACE, "Doomed")
    await put_item(db, doc("x", "x.md", scope))
    await put_item(db, doc("y", "y.md", scope))
    await db.commit()

    removed = await delete_collection(db, WORKSPACE, "doomed")
    await db.commit()

    # "Deleted: true" with no number is exactly the report that hides a scope
    # bug quietly deleting nothing at all.
    assert removed == 2
    assert await get_collection(db, WORKSPACE, "doomed") is None


# ---------------------------------- keys ----------------------------------


async def test_a_key_is_shown_once_and_then_only_as_a_prefix(db):
    issued = await create_key(db, WORKSPACE, "ci-pipeline")
    await db.commit()

    assert issued["key"].startswith("kb_live_")
    listed = await list_keys(db, WORKSPACE)
    assert len(listed) == 1
    # The full key exists nowhere we can read it back from.
    assert "key" not in listed[0]
    assert listed[0]["prefix"] in issued["key"]
    assert issued["key"] not in str(listed[0])


async def test_a_key_identifies_its_workspace(db):
    issued = await create_key(db, WORKSPACE, "sdk")
    await db.commit()

    holder = await resolve_key(db, issued["key"])
    await db.commit()
    assert holder is not None
    assert holder["workspace_id"] == WORKSPACE
    assert holder["scopes"] == ["read", "write"]


async def test_a_wrong_key_resolves_to_nothing(db):
    await create_key(db, WORKSPACE, "real")
    await db.commit()
    assert await resolve_key(db, "kb_live_not-a-real-key") is None


async def test_a_revoked_key_stops_working_immediately(db):
    issued = await create_key(db, WORKSPACE, "leaked")
    await db.commit()
    assert await resolve_key(db, issued["key"]) is not None
    await db.commit()

    assert await revoke_key(db, WORKSPACE, issued["key_id"]) is True
    await db.commit()

    assert await resolve_key(db, issued["key"]) is None
    # The row survives revocation so the audit trail is not erased with it.
    assert [k["revoked"] for k in await list_keys(db, WORKSPACE)] == [True]


async def test_a_read_only_key_says_so(db):
    issued = await create_key(db, WORKSPACE, "reader", scopes="read")
    await db.commit()
    holder = await resolve_key(db, issued["key"])
    await db.commit()
    assert holder is not None and holder["scopes"] == ["read"]


async def test_using_a_key_records_when(db):
    issued = await create_key(db, WORKSPACE, "watched")
    await db.commit()
    assert list(await list_keys(db, WORKSPACE))[0]["last_used_at"] is None

    await resolve_key(db, issued["key"])
    await db.commit()
    assert (await list_keys(db, WORKSPACE))[0]["last_used_at"] is not None
