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
#   (:Item {item_id, tenant_id, brand_id, title, status, source, embedding})
#     A lightweight mirror: the body stays in Postgres and is joined by id.
#     The node exists for two things only — vector recall, and relationships.
#   (:Item)-[:SUPERSEDES|DERIVES_FROM|REFERENCES]->(:Item)
#
# EVERY query filters on tenant_id. Where a brand is given it filters that too.
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
    clause = f"{alias}.tenant_id = $tenant"
    params: dict[str, Any] = {"tenant": scope.tenant_id}
    if scope.brand_id is not None:
        clause += f" AND {alias}.brand_id = $brand"
        params["brand"] = scope.brand_id
    return clause, params


# ------------------------------- bootstrap -------------------------------


async def bootstrap() -> None:
    """Constraints and indexes, created idempotently. Safe on every boot."""
    for stmt in [
        "CREATE CONSTRAINT item_id IF NOT EXISTS FOR (i:Item) REQUIRE i.item_id IS UNIQUE",
        "CREATE INDEX item_tenant IF NOT EXISTS FOR (i:Item) ON (i.tenant_id)",
        "CREATE INDEX item_scope IF NOT EXISTS FOR (i:Item) ON (i.tenant_id, i.brand_id)",
        f"""
        CREATE VECTOR INDEX {VECTOR_INDEX} IF NOT EXISTS
        FOR (i:Item) ON (i.embedding)
        OPTIONS {{indexConfig: {{
            `vector.dimensions`: {EMBED_DIM},
            `vector.similarity_function`: 'cosine'
        }}}}
        """,
    ]:
        await _run(stmt)


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
        SET i.tenant_id = $tenant,
            i.brand_id  = $brand,
            i.title     = $title,
            i.source    = $source,
            i.status    = $status
        """,
        item_id=item_id,
        tenant=scope.tenant_id,
        brand=scope.brand_id,
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
    """Nearest items by cosine similarity, scoped and filtered.

    The vector index itself is global, so this over-fetches and then applies
    tenancy before returning anything — the filter is never the caller's job.
    Superseded items are excluded by default so the agent cannot answer from
    content we already know has been replaced.
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
        RETURN node.item_id AS item_id, score AS similarity
        LIMIT $limit
        """,
        k=limit * 4,
        embedding=embedding,
        limit=limit,
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
               node.source AS source, node.brand_id AS brand_id, rels AS rels
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
                tenant_id=scope.tenant_id,
                brand_id=r["brand_id"],
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


async def wipe_tenant(tenant_id: str) -> int:
    """Remove every node for a tenant — offboarding and test teardown."""
    rows = await _run(
        "MATCH (n) WHERE n.tenant_id = $tenant DETACH DELETE n RETURN count(n) AS n",
        tenant=tenant_id,
    )
    return rows[0]["n"] if rows else 0
