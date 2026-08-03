from __future__ import annotations

from packages.core import graph
from packages.shared.schema import Lifecycle, Scope


async def test_chunk_vector_query_does_not_reference_absent_property_tokens(monkeypatch):
    """A fresh collection has no consolidation-only property keys yet.

    Static reads such as ``c.archived`` make Neo4j warn every time the graph
    view polls. Dynamic optional reads and the always-present status field keep
    the same defaults without producing schema warnings.
    """
    seen: dict[str, object] = {}

    async def capture(query: str, **params):
        seen["query"] = query
        seen["params"] = params
        return []

    monkeypatch.setattr(graph, "_run", capture)
    scope = Scope(workspace_id="workspace", collection_id="collection")

    assert await graph.collection_chunk_vectors(scope, live_only=True) == []

    query = str(seen["query"])
    assert "c.archived" not in query
    assert "c.node_type" not in query
    assert "c.stage" not in query
    assert "c.status = $active" in query
    assert "properties(c)['node_type']" in query
    assert "properties(c)['stage']" in query
    params = seen["params"]
    assert isinstance(params, dict)
    assert params["active"] == str(Lifecycle.ACTIVE)


async def test_lineage_reads_optional_archive_metadata_dynamically(monkeypatch):
    seen: dict[str, str] = {}

    async def capture(query: str, **params):
        seen["query"] = query
        return []

    monkeypatch.setattr(graph, "_run", capture)
    scope = Scope(workspace_id="workspace", collection_id="collection")

    assert await graph.chunk_lineage(scope, "chunk") == []
    assert "s.archived" not in seen["query"]
    assert "properties(s)['archived']" in seen["query"]
