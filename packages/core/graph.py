from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.schema import GraphEdge, GraphNode, GraphResult

# Graph lives in the same Postgres — nodes + edges tables. No Neo4j.

_UPSERT_NODE = text(
    """
    INSERT INTO nodes (id, company_id, type, key, label, source, metadata, updated_at)
    VALUES (:id, :company_id, :type, :key, :label, :source, CAST(:metadata AS jsonb), now())
    ON CONFLICT (id) DO UPDATE SET
        type = EXCLUDED.type,
        key = EXCLUDED.key,
        label = EXCLUDED.label,
        source = EXCLUDED.source,
        metadata = EXCLUDED.metadata,
        updated_at = now()
    """
)

_UPSERT_EDGE = text(
    """
    INSERT INTO edges (company_id, src_id, dst_id, type, weight, metadata)
    VALUES (:company_id, :src_id, :dst_id, :type, :weight, CAST(:metadata AS jsonb))
    ON CONFLICT (company_id, src_id, dst_id, type)
    DO UPDATE SET weight = GREATEST(edges.weight, EXCLUDED.weight)
    """
)

# Undirected N-hop reachability from a center node, company-scoped.
_TRAVERSE = text(
    """
    WITH RECURSIVE reach(id, depth) AS (
        SELECT CAST(:center AS text), 0
        UNION
        SELECT
            CASE WHEN e.src_id = r.id THEN e.dst_id ELSE e.src_id END,
            r.depth + 1
        FROM reach r
        JOIN edges e
          ON (e.src_id = r.id OR e.dst_id = r.id)
         AND e.company_id = :company_id
        WHERE r.depth < :hops
    )
    SELECT DISTINCT id FROM reach
    """
)


async def upsert_node(session: AsyncSession, node: GraphNode) -> None:
    await session.execute(
        _UPSERT_NODE,
        {
            "id": node.id,
            "company_id": node.company_id,
            "type": node.type,
            "key": node.key,
            "label": node.label,
            "source": node.source,
            "metadata": json.dumps(node.metadata),
        },
    )


async def upsert_edge(session: AsyncSession, edge: GraphEdge) -> None:
    await session.execute(
        _UPSERT_EDGE,
        {
            "company_id": edge.company_id,
            "src_id": edge.src_id,
            "dst_id": edge.dst_id,
            "type": edge.type,
            "weight": edge.weight,
            "metadata": json.dumps(edge.metadata),
        },
    )


def _node_from_row(r) -> GraphNode:
    md = r.metadata
    if isinstance(md, str):
        md = json.loads(md)
    return GraphNode(
        id=r.id,
        company_id=r.company_id,
        type=r.type,
        key=r.key,
        label=r.label,
        source=r.source,
        metadata=md or {},
    )


async def query_graph(
    session: AsyncSession,
    company_id: str,
    center_id: str,
    schema: dict | None = None,
    hops: int = 2,
) -> GraphResult:
    """Return the cluster within ``hops`` of ``center_id`` (nodes + edges)."""
    reached = await session.execute(
        _TRAVERSE, {"center": center_id, "company_id": company_id, "hops": hops}
    )
    ids = [row.id for row in reached]
    if center_id not in ids:
        ids.append(center_id)

    nodes_res = await session.execute(
        text(
            """
            SELECT id, company_id, type, key, label, source, metadata
            FROM nodes
            WHERE company_id = :c AND id = ANY(:ids)
            """
        ),
        {"c": company_id, "ids": ids},
    )
    nodes = [_node_from_row(r) for r in nodes_res]

    edges_res = await session.execute(
        text(
            """
            SELECT company_id, src_id, dst_id, type, weight, metadata
            FROM edges
            WHERE company_id = :c AND src_id = ANY(:ids) AND dst_id = ANY(:ids)
            """
        ),
        {"c": company_id, "ids": ids},
    )
    edges = []
    for r in edges_res:
        md = r.metadata
        if isinstance(md, str):
            md = json.loads(md)
        edges.append(
            GraphEdge(
                company_id=r.company_id,
                src_id=r.src_id,
                dst_id=r.dst_id,
                type=r.type,
                weight=float(r.weight),
                metadata=md or {},
            )
        )

    return GraphResult(center=center_id, nodes=nodes, edges=edges)
