"""The hard barrier: a collection-bound key cannot read another collection.

Written against a real incident on the client's side, not a hypothetical. Their
own graph memory is isolated by a `WHERE project_id = …` filter, and when a
workflow failed to resolve which client it was acting for, the store faithfully
returned another client's knowledge — the query was correct and the caller's id
was wrong. A WHERE clause cannot survive a caller-side bug.

So the guarantee here is deliberately stronger: the collection comes from the
CREDENTIAL, and no id the caller sends can move a request out of it. Every read
path is checked, because a barrier with one door open is not a barrier — and
three of them were open when this file was written:

  GET /api/chunks/{id}            scoped to the workspace, not the collection
  GET /api/chunks/{id}/lineage    same, and it returns source TEXT
  GET /api/chunks/{id}/neighbors  same, and the graph hop itself was unscoped
  GET /api/traces/{id}            same, and a trace holds the query and excerpts

These tests are the reason those cannot quietly come back.
"""

from __future__ import annotations

import inspect

import pytest

from apps.api import main
from packages.core import graph, tenancy, tracing

# ------------------------- the scope reaches every read -------------------------


def test_no_read_route_builds_its_own_workspace_wide_scope():
    """The bug pattern, banned by name.

    Three routes constructed `Scope(workspace_id=...)` by hand instead of
    depending on workspace_scope, and that single line is what let a bound key
    out of its collection. A route that needs a Scope must be GIVEN one.
    """
    source = inspect.getsource(main)
    # Only the workspace-WIDE form is banned. The /api/collections/{id}/…
    # routes legitimately build a Scope from the path, because the path names
    # the collection and enforce_binding has already checked it against the
    # key's binding — there the collection is present, not missing.
    handwritten = source.count('Scope(workspace_id=str(principal["company_id"]))')
    assert handwritten == 0, (
        "a route is building a workspace-wide Scope from the principal; "
        "depend on workspace_scope instead"
    )


def test_every_content_route_takes_a_scope():
    """Anything that can return document content resolves its scope through
    the dependency that honours the binding."""
    from apps.api import authz

    content_routes = {
        path
        for (method, path), needed in authz.matrix().items()
        if needed == authz.READ and method == "GET"
    }
    # Routes whose scope arrives another way: the collection is named in the
    # path (or, for snippets, in a query parameter) and enforce_binding checks
    # it against the key's binding there.
    by_path = {p for p in content_routes if p.startswith("/api/collections/")}
    by_path.add("/api/snippets")

    missing = []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        if path not in content_routes or path in by_path:
            continue
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        params = inspect.signature(endpoint).parameters
        if "scope" not in params:
            missing.append(path)
    assert not missing, f"content routes with no scope dependency: {missing}"


# ---------------------------- the graph hops ----------------------------


def test_a_neighbour_hop_is_scoped_at_both_ends():
    """R1.2, exactly as the client wrote it: a NEAR edge must never traverse
    into another collection's chunk.

    Scoping only the chunk you start from lets one edge carry the answer out of
    the collection. An edge is not permission to cross a boundary.
    """
    source = inspect.getsource(graph.chunk_neighbours)
    assert '_scope_clause("c", scope)' in source
    assert '_scope_clause("n", scope)' in source


def test_lineage_is_scoped_at_both_ends():
    """DERIVED_FROM returns the source passages behind a written summary — the
    evidence, in full — so the far end needs scoping just as much."""
    source = inspect.getsource(graph.chunk_lineage)
    assert '_scope_clause("c", scope)' in source
    assert '_scope_clause("s", scope)' in source


def test_the_scope_clause_actually_filters_on_the_collection():
    scope_all = tenancy.Scope(workspace_id="w")
    scope_one = tenancy.Scope(workspace_id="w", collection_id="client-a")
    clause_all, _ = graph._scope_clause("n", scope_all)
    clause_one, params = graph._scope_clause("n", scope_one)
    assert "collection_id" not in clause_all
    assert "n.collection_id = $collection" in clause_one
    assert params["collection"] == "client-a"


# ------------------------------- traces -------------------------------


def test_fetching_one_trace_filters_on_the_collection():
    """A trace holds the question somebody typed and excerpts of the passages
    that answered it. list_traces filtered on the collection from the start;
    get_trace did not, which made the listing's scoping decorative to anyone
    holding an id."""
    source = inspect.getsource(tracing.get_trace)
    # CAST because asyncpg cannot infer the type of a parameter that only
    # appears in an IS NULL test — without it this route answered 500, which
    # the live probe caught and the source-level test had happily passed.
    assert "CAST(:coll AS text) IS NULL OR collection_id = :coll" in source


# ------------------------- minting cannot widen -------------------------


def test_a_bound_key_cannot_mint_an_unbound_one():
    """Otherwise the binding is one API call deep: a key confined to A mints an
    unbound key and reads B with it."""
    source = inspect.getsource(main.add_key)
    assert "minter_collection" in source
    from packages.core import keys

    assert "minter_collection" in inspect.getsource(keys.create_key)


def test_the_scope_check_and_the_binding_check_are_both_present():
    """One stops a key becoming more powerful, the other stops it becoming less
    confined. A binding needs both."""
    from packages.core import keys

    source = inspect.getsource(keys.create_key)
    assert "check_subset(minter_scopes, wanted)" in source
    assert "raise Escalation" in source


# --------------------------- the refusal is recorded ---------------------------


def test_a_refused_cross_collection_read_is_audited():
    """R1.4. The caller who trips this is usually not an attacker — it is a
    workflow that failed to resolve which client it was acting for, which is
    the bug a hard barrier exists to make visible. Blocking it is half the job.
    """
    handler = inspect.getsource(main.record_cross_collection_attempt)
    assert "audit.record" in handler
    assert '"collection.access_denied"' in handler
    for field in ("bound_collection", "requested_collection", "key_id", "path"):
        assert field in handler


def test_recording_the_refusal_cannot_change_the_answer():
    """A failure to write telemetry must never turn a 403 into a 500."""
    handler = inspect.getsource(main.record_cross_collection_attempt)
    assert "except Exception" in handler
    assert "status_code=403" in handler


def test_both_binding_checks_raise_the_recordable_error():
    """The header path and the URL-path path both refuse, and both are
    recorded — one of them raising a bare HTTPException would be a blind spot
    in exactly the audit trail this exists for."""
    assert "CrossCollectionAttempt" in inspect.getsource(tenancy.enforce_binding)
    assert "CrossCollectionAttempt" in inspect.getsource(tenancy.workspace_scope)


# ------------------------ selection narrows, never widens ------------------------


def test_file_selection_composes_with_the_binding():
    """R1.3: a key bound to A passing file ids from B gets nothing, not B's
    files. The item filter is ANDed with the scope filter rather than replacing
    it."""
    from packages.core import search

    source = inspect.getsource(search._filters)
    assert "item_id = ANY(:item_ids)" in source
    assert "workspace_id" in source and "collection_id" in source


@pytest.mark.needs_db
async def test_a_bound_scope_cannot_read_another_collections_passage(db):
    """The end-to-end property, in the database rather than in the source.

    Two collections, one passage in each. A scope bound to the first must not
    be able to hydrate the second's passage by id.
    """
    from sqlalchemy import text

    from packages.core.chunks import by_ids
    from packages.shared.schema import Scope
    from tests.conftest import SCOPE

    for collection, chunk in (("iso-a", "chunk-a"), ("iso-b", "chunk-b")):
        await db.execute(
            text(
                """
                INSERT INTO kb_chunks (chunk_id, workspace_id, collection_id,
                                       item_id, version, ordinal, heading, text)
                VALUES (:c, :w, :coll, :i, 1, 0, 'h', 'the secret')
                """
            ),
            {"c": chunk, "w": SCOPE.workspace_id, "coll": collection, "i": f"item-{collection}"},
        )

    bound_to_a = Scope(workspace_id=SCOPE.workspace_id, collection_id="iso-a")
    got = await by_ids(db, bound_to_a, ["chunk-a", "chunk-b"])
    assert "chunk-a" in got, "its own passage must still be readable"
    assert "chunk-b" not in got, "another collection's passage must not be"
