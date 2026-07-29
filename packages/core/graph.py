from __future__ import annotations

import asyncio
import os
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase

from packages.shared.schema import GraphEdge, GraphNode, GraphResult, Lifecycle, Link, Scope

# ============================================================================
# THE graph module. Every Cypher string in the codebase lives HERE — one place,
# the same rule as the single LLM client.
#
# Data model (Neo4j 5):
#   (:Item {item_id, workspace_id, collection_id, title, status, source, embedding})
#     A lightweight mirror: the body stays in Postgres and is joined by id.
#     The node exists for two things only — vector recall, and relationships.
#   (:Item)-[:SUPERSEDES|DERIVES_FROM|REFERENCES]->(:Item)
#
# EVERY query filters on workspace_id. Where a collection is given it filters that too.
# Tenancy is not optional and is not left to the caller to remember.
# ============================================================================

EMBED_DIM = int(os.getenv("EMBED_DIM", "1536"))
VECTOR_INDEX = "item_embedding_index"

_driver: AsyncDriver | None = None
_driver_loop: asyncio.AbstractEventLoop | None = None


def get_driver() -> AsyncDriver:
    """Async driver, one per event loop.

    Loop-aware because pytest gives every test its own loop, and a driver whose
    connections are bound to a finished loop raises on the next await.
    """
    global _driver, _driver_loop
    loop = asyncio.get_running_loop()
    if _driver is None or _driver_loop is not loop:
        _driver = AsyncGraphDatabase.driver(
            os.getenv("NEO4J_URI", "bolt://localhost:7688"),
            auth=(os.getenv("NEO4J_USER", "neo4j"), os.getenv("NEO4J_PASSWORD", "markos-graph")),
        )
        _driver_loop = loop
    return _driver


async def close_driver() -> None:
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


async def _run(query: str, **params: Any) -> list[dict]:
    async with get_driver().session() as session:
        result = await session.run(query, **params)
        return [dict(record) async for record in result]


def _rel(link: Link | str) -> str:
    """A relationship type is interpolated into Cypher (it cannot be a
    parameter), so it must come from our own closed vocabulary."""
    name = link.value if isinstance(link, Link) else str(link)
    if name not in {member.value for member in Link}:
        raise ValueError(f"unknown link type: {name!r}")
    return name


def _scope_clause(alias: str, scope: Scope) -> tuple[str, dict[str, Any]]:
    """Tenancy, applied identically everywhere it is needed."""
    clause = f"{alias}.workspace_id = $workspace"
    params: dict[str, Any] = {"workspace": scope.workspace_id}
    if scope.collection_id is not None:
        clause += f" AND {alias}.collection_id = $collection"
        params["collection"] = scope.collection_id
    return clause, params


# ------------------------------- bootstrap -------------------------------


async def migrate_legacy_properties() -> int:
    """Carry nodes written before the scope rename onto the new property names.

    Postgres columns were renamed by a migration; graph properties have no such
    mechanism, so nodes written as `tenant_id`/`brand_id` simply stopped
    matching any scope filter — present in the store, invisible to search, with
    no error anywhere. Found by reading a query trace that showed one semantic
    candidate where there should have been nine.

    Idempotent: only touches nodes that have not been carried over yet.
    """
    rows = await _run(
        """
        MATCH (i:Item)
        WHERE i.tenant_id IS NOT NULL AND i.workspace_id IS NULL
        SET i.workspace_id = i.tenant_id,
            i.collection_id = i.brand_id
        REMOVE i.tenant_id, i.brand_id
        RETURN count(i) AS n
        """
    )
    return rows[0]["n"] if rows else 0


async def _move_vector_index_to_chunks() -> bool:
    """Retarget the vector index from Item onto Chunk.

    `CREATE VECTOR INDEX ... IF NOT EXISTS` matches on the NAME, so an index
    already defined over (:Item) satisfies the new definition and the create is
    silently skipped — leaving passages unindexed and semantic recall finding
    nothing, with no error to read. The old one has to be dropped by name
    first. Idempotent: once it is on Chunk there is nothing to do.
    """
    rows = await _run(
        "SHOW VECTOR INDEXES YIELD name, labelsOrTypes WHERE name = $name "
        "RETURN labelsOrTypes AS labels",
        name=VECTOR_INDEX,
    )
    if not rows or "Item" not in (rows[0].get("labels") or []):
        return False
    await _run(f"DROP INDEX {VECTOR_INDEX} IF EXISTS")
    return True


async def bootstrap() -> None:
    """Constraints and indexes, created idempotently. Safe on every boot."""
    await _move_vector_index_to_chunks()
    for stmt in [
        "CREATE CONSTRAINT item_id IF NOT EXISTS FOR (i:Item) REQUIRE i.item_id IS UNIQUE",
        "CREATE INDEX item_tenant IF NOT EXISTS FOR (i:Item) ON (i.workspace_id)",
        "CREATE INDEX item_scope IF NOT EXISTS FOR (i:Item) ON (i.workspace_id, i.collection_id)",
        "CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.chunk_id IS UNIQUE",
        "CREATE INDEX chunk_scope IF NOT EXISTS FOR (c:Chunk) ON (c.workspace_id, c.collection_id)",
        "CREATE INDEX chunk_item IF NOT EXISTS FOR (c:Chunk) ON (c.workspace_id, c.item_id)",
        # The vector index lives on the passage, not the document. It is what
        # makes recall land on a paragraph instead of a whole file.
        f"""
        CREATE VECTOR INDEX {VECTOR_INDEX} IF NOT EXISTS
        FOR (c:Chunk) ON (c.embedding)
        OPTIONS {{indexConfig: {{
            `vector.dimensions`: {EMBED_DIM},
            `vector.similarity_function`: 'cosine'
        }}}}
        """,
    ]:
        await _run(stmt)
    # Runs on every boot so a deploy carries legacy nodes across without
    # anyone having to remember a one-off script.
    await migrate_legacy_properties()


# --------------------------------- items ---------------------------------


async def upsert_item(
    scope: Scope,
    item_id: str,
    title: str,
    source: str,
    status: str = Lifecycle.ACTIVE,
    embedding: list[float] | None = None,
) -> None:
    """Mirror an item into the graph. Body stays in Postgres."""
    await _run(
        """
        MERGE (i:Item {item_id: $item_id})
        SET i.workspace_id = $workspace,
            i.collection_id  = $collection,
            i.title     = $title,
            i.source    = $source,
            i.status    = $status
        """,
        item_id=item_id,
        workspace=scope.workspace_id,
        collection=scope.collection_id,
        title=title,
        source=source,
        status=str(status),
    )
    if embedding is not None:
        await set_embedding(scope, item_id, embedding)


async def set_embedding(scope: Scope, item_id: str, embedding: list[float]) -> None:
    clause, params = _scope_clause("i", scope)
    await _run(
        f"""
        MATCH (i:Item {{item_id: $item_id}}) WHERE {clause}
        CALL db.create.setNodeVectorProperty(i, 'embedding', $embedding)
        """,
        item_id=item_id,
        embedding=embedding,
        **params,
    )


async def set_status(scope: Scope, item_id: str, status: str) -> None:
    """Keep the mirror's lifecycle in step so superseded items leave recall."""
    clause, params = _scope_clause("i", scope)
    await _run(
        f"MATCH (i:Item {{item_id: $item_id}}) WHERE {clause} SET i.status = $status",
        item_id=item_id,
        status=str(status),
        **params,
    )


async def link_items(scope: Scope, src_id: str, dst_id: str, link: Link | str) -> None:
    """A typed relationship between two items, both inside the same scope."""
    rel = _rel(link)
    clause_a, params = _scope_clause("a", scope)
    clause_b, _ = _scope_clause("b", scope)
    await _run(
        f"""
        MATCH (a:Item {{item_id: $src}}) WHERE {clause_a}
        MATCH (b:Item {{item_id: $dst}}) WHERE {clause_b}
        MERGE (a)-[:{rel}]->(b)
        """,
        src=src_id,
        dst=dst_id,
        **params,
    )


# -------------------------------- queries --------------------------------


async def vector_search(
    scope: Scope, embedding: list[float], limit: int = 20, active_only: bool = True
) -> list[dict]:
    """Nearest passages by cosine similarity, scoped and filtered.

    The vector index itself is global, so this over-fetches and then applies
    tenancy before returning anything — the filter is never the caller's job.
    Superseded items are excluded by default so the agent cannot answer from
    content we already know has been replaced.

    Returns passages. The caller decides whether to present them as passages
    or roll them up into the documents they belong to; both need to know which
    passage matched, because that is the citation.
    """
    clause, params = _scope_clause("node", scope)
    if active_only:
        clause += " AND node.status = $active"
        params["active"] = str(Lifecycle.ACTIVE)
    return await _run(
        f"""
        CALL db.index.vector.queryNodes('{VECTOR_INDEX}', $k, $embedding)
        YIELD node, score
        WHERE {clause}
        RETURN node.chunk_id AS chunk_id, node.item_id AS item_id,
               node.ordinal AS ordinal, score AS similarity
        LIMIT $limit
        """,
        # Over-fetch harder than the item index did: several passages of one
        # document can occupy the top of the list, so the raw neighbour count
        # no longer approximates the number of distinct documents.
        k=limit * 8,
        embedding=embedding,
        limit=limit,
        **params,
    )


# -------------------------------- passages --------------------------------


async def replace_chunks(scope: Scope, item_id: str, chunks: list[dict]) -> int:
    """Swap a document's passages for a new set, in one transaction.

    Delete-then-write rather than merge: passage boundaries move when a
    document is edited, so reconciling one by one would leave orphans behind
    that still answer queries. Each dict carries chunk_id, ordinal, heading,
    title and embedding.
    """
    clause, params = _scope_clause("c", scope)
    await _run(
        f"MATCH (c:Chunk {{item_id: $item_id}}) WHERE {clause} DETACH DELETE c",
        item_id=item_id,
        **params,
    )
    if not chunks:
        return 0
    rows = await _run(
        """
        UNWIND $chunks AS row
        CREATE (c:Chunk {chunk_id: row.chunk_id})
        SET c.workspace_id  = $workspace,
            c.collection_id = $collection,
            c.item_id       = $item_id,
            c.ordinal       = row.ordinal,
            c.heading       = row.heading,
            c.title         = row.title,
            c.status        = $status
        WITH c, row
        CALL db.create.setNodeVectorProperty(c, 'embedding', row.embedding)
        WITH c
        MATCH (i:Item {item_id: $item_id})
        MERGE (c)-[:PART_OF]->(i)
        RETURN count(c) AS n
        """,
        chunks=chunks,
        item_id=item_id,
        workspace=scope.workspace_id,
        collection=scope.collection_id,
        status=str(Lifecycle.ACTIVE),
    )
    return rows[0]["n"] if rows else 0


async def collection_chunk_vectors(
    scope: Scope, limit: int = 400, live_only: bool = False
) -> list[dict]:
    """Every passage in a collection with its vector, for the graph view.

    ``live_only`` excludes archived nodes — consolidation must never fold an
    archived node back into a fresh summary, which would resurrect content the
    store has already decided is gone.
    """
    clause, params = _scope_clause("c", scope)
    if live_only:
        clause += " AND c.archived IS NULL"
    return await _run(
        f"""
        MATCH (c:Chunk) WHERE {clause} AND c.embedding IS NOT NULL
        RETURN c.chunk_id AS id, c.item_id AS item_id, c.ordinal AS ordinal,
               c.heading AS heading, c.title AS title, c.embedding AS embedding,
               coalesce(c.node_type, 'fact') AS node_type,
               coalesce(c.stage, 1) AS stage
        ORDER BY c.item_id, c.ordinal
        LIMIT $limit
        """,
        limit=limit,
        **params,
    )


async def upsert_summary(
    scope: Scope,
    chunk_id: str,
    heading: str,
    embedding: list[float],
    sources: list[str],
) -> None:
    """A node the store wrote, standing for the passages it replaced.

    DERIVED_FROM edges to its members are kept even though the members are
    archived: provenance has to outlive the thing it explains, or a summary
    becomes an assertion nobody can check.
    """
    await _run(
        """
        MERGE (c:Chunk {chunk_id: $chunk_id})
        SET c.workspace_id  = $workspace,
            c.collection_id = $collection,
            c.heading       = $heading,
            c.title         = $heading,
            c.node_type     = 'summary',
            c.status        = $status,
            c.ordinal       = 0,
            c.archived      = NULL
        """,
        chunk_id=chunk_id,
        workspace=scope.workspace_id,
        collection=scope.collection_id,
        heading=heading,
        status=str(Lifecycle.ACTIVE),
    )
    await _run(
        """
        MATCH (c:Chunk {chunk_id: $chunk_id})
        CALL db.create.setNodeVectorProperty(c, 'embedding', $embedding)
        """,
        chunk_id=chunk_id,
        embedding=embedding,
    )
    await _run(
        """
        MATCH (c:Chunk {chunk_id: $chunk_id})
        UNWIND $sources AS source_id
        MATCH (s:Chunk {chunk_id: source_id})
        MERGE (c)-[:DERIVED_FROM]->(s)
        """,
        chunk_id=chunk_id,
        sources=sources,
    )


async def archive_chunk(scope: Scope, chunk_id: str) -> None:
    """Take a node out of retrieval without destroying it.

    The vector is removed but the node stays, so DERIVED_FROM still resolves
    and a summary can name what it came from. Removing the embedding is what
    takes it out of recall — the index has no other filter.
    """
    clause, params = _scope_clause("c", scope)
    await _run(
        f"""
        MATCH (c:Chunk {{chunk_id: $chunk_id}}) WHERE {clause}
        SET c.archived = timestamp(), c.status = 'archived'
        REMOVE c.embedding
        """,
        chunk_id=chunk_id,
        **params,
    )


async def delete_chunk(scope: Scope, chunk_id: str) -> None:
    """Fully decayed: the node goes, and its edges with it."""
    clause, params = _scope_clause("c", scope)
    await _run(
        f"MATCH (c:Chunk {{chunk_id: $chunk_id}}) WHERE {clause} DETACH DELETE c",
        chunk_id=chunk_id,
        **params,
    )


async def chunk_lineage(scope: Scope, chunk_id: str) -> list[dict]:
    """What a summary was built from — the evidence behind a written claim."""
    clause, params = _scope_clause("c", scope)
    return await _run(
        f"""
        MATCH (c:Chunk {{chunk_id: $chunk_id}})-[:DERIVED_FROM]->(s:Chunk)
        WHERE {clause}
        RETURN s.chunk_id AS chunk_id, s.heading AS heading,
               s.item_id AS item_id, s.archived AS archived
        """,
        chunk_id=chunk_id,
        **params,
    )


async def related(scope: Scope, item_id: str, hops: int = 1) -> GraphResult:
    """What this item connects to — lineage and derivation, scoped."""
    hops = max(1, min(int(hops), 3))
    clause, params = _scope_clause("n", scope)
    centre_clause, _ = _scope_clause("centre", scope)
    rows = await _run(
        f"""
        MATCH (centre:Item {{item_id: $item_id}}) WHERE {centre_clause}
        OPTIONAL MATCH path = (centre)-[*1..{hops}]-(n:Item)
        WHERE {clause}
        WITH centre, collect(DISTINCT n) AS reached
        WITH [centre] + reached AS cluster
        UNWIND cluster AS node
        WITH collect(DISTINCT node) AS cluster
        CALL {{
            WITH cluster
            UNWIND cluster AS a
            MATCH (a)-[r]->(b:Item)
            WHERE b IN cluster
            RETURN collect(DISTINCT {{src: a.item_id, dst: b.item_id, type: type(r)}}) AS rels
        }}
        UNWIND cluster AS node
        RETURN node.item_id AS item_id, node.title AS title, node.status AS status,
               node.source AS source, node.collection_id AS collection_id, rels AS rels
        """,
        item_id=item_id,
        **params,
    )

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen = False
    for r in rows:
        if r["item_id"] is None:
            continue
        nodes.append(
            GraphNode(
                id=r["item_id"],
                workspace_id=scope.workspace_id,
                collection_id=r["collection_id"],
                title=r["title"] or r["item_id"],
                status=r["status"] or Lifecycle.ACTIVE,
                source=r["source"],
            )
        )
        if not seen:
            for rel in r["rels"] or []:
                edges.append(GraphEdge(src_id=rel["src"], dst_id=rel["dst"], type=rel["type"]))
            seen = True
    return GraphResult(center=item_id, nodes=nodes, edges=edges)


async def collection_vectors(scope: Scope, limit: int = 200) -> list[dict]:
    """Every item in scope with its embedding, for building a neighbour graph.

    Returned in one query rather than one per item: a k-nearest-neighbour map
    over 200 points is 200 vector-index probes if done node by node, which is
    slower than fetching the vectors once and comparing them in memory.
    """
    clause, params = _scope_clause("i", scope)
    return await _run(
        f"""
        MATCH (i:Item)
        WHERE {clause} AND i.status = $active AND i.embedding IS NOT NULL
        RETURN i.item_id AS id, i.title AS title, i.source AS source,
               i.embedding AS embedding
        LIMIT $limit
        """,
        active=str(Lifecycle.ACTIVE),
        limit=limit,
        **params,
    )


async def links_between(scope: Scope, item_ids: list[str]) -> list[dict]:
    """Declared relationships among a set of items — lineage and derivation.

    These are different in kind from similarity edges: one is something the
    store recorded, the other is something it computed, and the view must not
    blur them together.
    """
    if not item_ids:
        return []
    clause_a, params = _scope_clause("a", scope)
    clause_b, _ = _scope_clause("b", scope)
    return await _run(
        f"""
        MATCH (a:Item)-[r]->(b:Item)
        WHERE {clause_a} AND {clause_b}
          AND a.item_id IN $ids AND b.item_id IN $ids
        RETURN a.item_id AS src, b.item_id AS dst, type(r) AS type
        """,
        ids=item_ids,
        **params,
    )


async def count_nodes(scope: Scope | None = None) -> dict[str, int]:
    if scope is None:
        rows = await _run("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS n")
    else:
        clause, params = _scope_clause("n", scope)
        rows = await _run(
            f"MATCH (n) WHERE {clause} RETURN labels(n)[0] AS label, count(n) AS n", **params
        )
    out = {r["label"]: r["n"] for r in rows if r["label"]}
    out["total"] = sum(out.values())
    return out


async def wipe_tenant(workspace_id: str) -> int:
    """Remove every node for a workspace — offboarding and test teardown."""
    rows = await _run(
        "MATCH (n) WHERE n.workspace_id = $workspace DETACH DELETE n RETURN count(n) AS n",
        workspace=workspace_id,
    )
    return rows[0]["n"] if rows else 0
