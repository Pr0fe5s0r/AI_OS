from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase

from packages.shared.schema import GraphEdge, GraphNode, GraphResult

# ============================================================================
# THE graph module. Every Cypher string in the entire codebase lives HERE —
# the same rule as the single LLM client. Callers get plain functions.
#
# Data model (Neo4j 5, community):
#   (:Thing {id, company_id, thing_type, title, status, last_activity, ...})
#   (:Event {id, company_id, event_time, source, embedding})   <- lightweight
#     mirror; full event content stays in Postgres, joined by id.
#   (:Event)-[:ABOUT]->(:Thing)
#   (:Thing)-[<typed from profile data>]->(:Thing)  e.g. CLOSES, CAUSED_BY,
#   CONTAINS, SAME_AS {confidence} — the engine never hardcodes these names.
#
# EVERY query filters on company_id. Tenancy is not optional.
# ============================================================================

EMBED_DIM = int(os.getenv("EMBED_DIM", "1536"))
VECTOR_INDEX = "event_embedding_index"

_driver: AsyncDriver | None = None
_driver_loop: asyncio.AbstractEventLoop | None = None


def get_driver() -> AsyncDriver:
    """Async driver, one per event loop. Credentials come from env.

    Loop-aware for the same reason db.py uses NullPool: pytest gives every
    test its own event loop, and a driver whose connections are bound to a
    finished loop raises RuntimeError on the next await.
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


# A relationship type is interpolated into Cypher (parameters can't name a
# rel type), so it must be validated first — profile data, not user free-text.
def _rel(rel_type: str) -> str:
    if not rel_type.replace("_", "").isalnum() or not rel_type[0].isalpha():
        raise ValueError(f"invalid relationship type: {rel_type!r}")
    return rel_type.upper()


# ------------------------------- bootstrap -------------------------------


async def bootstrap() -> None:
    """Create constraints + indexes idempotently. Safe to run on every boot."""
    statements = [
        "CREATE CONSTRAINT thing_id IF NOT EXISTS FOR (t:Thing) REQUIRE t.id IS UNIQUE",
        "CREATE CONSTRAINT event_id IF NOT EXISTS FOR (e:Event) REQUIRE e.id IS UNIQUE",
        "CREATE INDEX thing_company IF NOT EXISTS FOR (t:Thing) ON (t.company_id)",
        "CREATE INDEX event_company IF NOT EXISTS FOR (e:Event) ON (e.company_id)",
        "CREATE INDEX thing_type IF NOT EXISTS FOR (t:Thing) ON (t.company_id, t.thing_type)",
        f"""
        CREATE VECTOR INDEX {VECTOR_INDEX} IF NOT EXISTS
        FOR (e:Event) ON (e.embedding)
        OPTIONS {{indexConfig: {{
            `vector.dimensions`: {EMBED_DIM},
            `vector.similarity_function`: 'cosine'
        }}}}
        """,
    ]
    for stmt in statements:
        await _run(stmt)


# ----------------------------- events (mirror) -----------------------------


async def mirror_event(
    company_id: str,
    event_id: str,
    event_time: datetime,
    source: str,
    embedding: list[float] | None = None,
) -> None:
    """Upsert the lightweight :Event mirror node (full content is in Postgres)."""
    await _run(
        """
        MERGE (e:Event {id: $id})
        SET e.company_id = $company_id,
            e.event_time = $event_time,
            e.source = $source
        """,
        id=event_id,
        company_id=company_id,
        event_time=event_time.isoformat(),
        source=source,
    )
    if embedding is not None:
        await set_event_embedding(company_id, event_id, embedding)


async def set_event_embedding(company_id: str, event_id: str, embedding: list[float]) -> None:
    await _run(
        """
        MATCH (e:Event {id: $id, company_id: $company_id})
        CALL db.create.setNodeVectorProperty(e, 'embedding', $embedding)
        """,
        id=event_id,
        company_id=company_id,
        embedding=embedding,
    )


async def event_embedding(company_id: str, event_id: str) -> list[float] | None:
    rows = await _run(
        "MATCH (e:Event {id: $id, company_id: $company_id}) RETURN e.embedding AS emb",
        id=event_id,
        company_id=company_id,
    )
    return rows[0]["emb"] if rows and rows[0]["emb"] is not None else None


# -------------------------------- things --------------------------------


async def upsert_thing(
    company_id: str,
    thing_id: str,
    thing_type: str,
    title: str,
    status: str | None = None,
    last_activity: datetime | None = None,
    properties: dict[str, Any] | None = None,
) -> None:
    """Upsert a :Thing. `properties` may only contain primitive values."""
    props = {
        k: v for k, v in (properties or {}).items()
        if isinstance(v, str | int | float | bool) or v is None
    }
    await _run(
        """
        MERGE (t:Thing {id: $id})
        SET t.company_id = $company_id,
            t.thing_type = $thing_type,
            t.title = $title,
            t.status = coalesce($status, t.status),
            t.last_activity = coalesce($last_activity, t.last_activity),
            t += $props
        """,
        id=thing_id,
        company_id=company_id,
        thing_type=thing_type,
        title=title,
        status=status,
        last_activity=last_activity.isoformat() if last_activity else None,
        props=props,
    )


async def link_event_to_thing(company_id: str, event_id: str, thing_id: str) -> None:
    await _run(
        """
        MATCH (e:Event {id: $event_id, company_id: $c})
        MATCH (t:Thing {id: $thing_id, company_id: $c})
        MERGE (e)-[:ABOUT]->(t)
        """,
        event_id=event_id,
        thing_id=thing_id,
        c=company_id,
    )


async def link_things(
    company_id: str,
    src_id: str,
    dst_id: str,
    rel_type: str,
    confidence: float = 1.0,
    method: str | None = None,
) -> None:
    """Typed thing->thing link. The type comes from profile data (validated)."""
    rel = _rel(rel_type)
    await _run(
        f"""
        MATCH (a:Thing {{id: $src, company_id: $c}})
        MATCH (b:Thing {{id: $dst, company_id: $c}})
        MERGE (a)-[r:{rel}]->(b)
        SET r.confidence = CASE WHEN r.confidence IS NULL OR r.confidence < $confidence
                                THEN $confidence ELSE r.confidence END,
            r.method = coalesce($method, r.method)
        """,
        src=src_id,
        dst=dst_id,
        c=company_id,
        confidence=confidence,
        method=method,
    )


# ------------------------------- queries -------------------------------


async def vector_search(
    company_id: str, embedding: list[float], limit: int = 20
) -> list[dict]:
    """Nearest :Event nodes by cosine similarity, scoped to the company.

    Over-fetches then filters on company_id because the vector index itself is
    global — the filter is applied before anything is returned to the caller.
    """
    rows = await _run(
        f"""
        CALL db.index.vector.queryNodes('{VECTOR_INDEX}', $k, $embedding)
        YIELD node, score
        WHERE node.company_id = $company_id
        RETURN node.id AS event_id, score AS similarity
        LIMIT $limit
        """,
        k=limit * 4,
        embedding=embedding,
        company_id=company_id,
        limit=limit,
    )
    return rows


async def query_graph(
    company_id: str, center_id: str, hops: int = 2
) -> GraphResult:
    """Everything within `hops` of a node (Thing or Event), company-scoped.

    Returns the same GraphResult shape the API/UI already speak.
    """
    hops = max(1, min(int(hops), 4))
    rows = await _run(
        f"""
        MATCH (center {{id: $center, company_id: $c}})
        CALL {{
            WITH center
            MATCH p = (center)-[*1..{hops}]-(n)
            WHERE all(x IN nodes(p) WHERE x.company_id = $c)
            RETURN collect(DISTINCT n) AS reached
        }}
        WITH center, reached + [center] AS cluster
        UNWIND cluster AS node
        WITH collect(DISTINCT node) AS cluster
        CALL {{
            WITH cluster
            UNWIND cluster AS a
            MATCH (a)-[r]->(b)
            WHERE b IN cluster
            RETURN collect(DISTINCT {{
                src: a.id, dst: b.id, type: type(r),
                confidence: coalesce(r.confidence, 1.0),
                method: r.method
            }}) AS rels
        }}
        UNWIND cluster AS node
        RETURN
            node.id AS id,
            labels(node)[0] AS label,
            node.thing_type AS thing_type,
            node.title AS title,
            node.status AS status,
            node.source AS source,
            node.event_time AS event_time,
            node.last_activity AS last_activity,
            rels AS rels
        """,
        center=center_id,
        c=company_id,
    )

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen_rels = False
    for r in rows:
        nodes.append(
            GraphNode(
                id=r["id"],
                company_id=company_id,
                type=r["thing_type"] or r["label"],
                key=r["id"],
                label=r["title"] or r["id"],
                source=r["source"],
                metadata={
                    "status": r["status"],
                    "event_time": r["event_time"],
                    "last_activity": r["last_activity"],
                    "node_kind": r["label"],
                },
            )
        )
        if not seen_rels:
            for rel in r["rels"] or []:
                edges.append(
                    GraphEdge(
                        company_id=company_id,
                        src_id=rel["src"],
                        dst_id=rel["dst"],
                        type=rel["type"],
                        weight=float(rel["confidence"]),
                        metadata={"method": rel["method"]} if rel["method"] else {},
                    )
                )
            seen_rels = True
    return GraphResult(center=center_id, nodes=nodes, edges=edges)


async def company_graph(company_id: str, limit: int = 300) -> dict[str, list[dict]]:
    """The whole picture: every Thing, how busy it is, and what connects to what.

    Unlike query_graph() this has no centre — it is the map, not a neighbourhood.
    Event counts come back with the nodes so the UI can size them by activity
    without a second round-trip per node. Busiest things win the limit, because
    a truncated graph should keep the part a person came to look at.
    """
    things = await _run(
        """
        MATCH (t:Thing {company_id: $c})
        OPTIONAL MATCH (e:Event {company_id: $c})-[:ABOUT]->(t)
        WITH t, count(e) AS events
        RETURN t.id AS id, t.thing_type AS thing_type, t.title AS title,
               t.status AS status, t.last_activity AS last_activity,
               t.source AS source, events AS events
        ORDER BY events DESC, t.last_activity DESC
        LIMIT $limit
        """,
        c=company_id,
        limit=limit,
    )
    ids = [t["id"] for t in things]
    if not ids:
        return {"things": [], "links": []}

    links = await _run(
        """
        MATCH (a:Thing {company_id: $c})-[r]->(b:Thing {company_id: $c})
        WHERE a.id IN $ids AND b.id IN $ids
        RETURN a.id AS src, b.id AS dst, type(r) AS type,
               coalesce(r.confidence, 1.0) AS confidence
        """,
        c=company_id,
        ids=ids,
    )
    return {"things": things, "links": links}


async def thing_detail(company_id: str, thing_id: str, limit: int = 50) -> dict:
    """One Thing, its neighbours, and the events that mention it.

    Event *content* lives in Postgres — this returns ids and times only, and the
    caller joins. Keeping the mirror thin is the whole reason it stays in sync.
    """
    rows = await _run(
        """
        MATCH (t:Thing {company_id: $c, id: $id})
        RETURN t.id AS id, t.thing_type AS thing_type, t.title AS title,
               t.status AS status, t.last_activity AS last_activity, t.source AS source
        """,
        c=company_id,
        id=thing_id,
    )
    if not rows:
        return {}

    events = await _run(
        """
        MATCH (e:Event {company_id: $c})-[:ABOUT]->(:Thing {company_id: $c, id: $id})
        RETURN e.id AS event_id, e.event_time AS event_time, e.source AS source
        ORDER BY e.event_time DESC
        LIMIT $limit
        """,
        c=company_id,
        id=thing_id,
        limit=limit,
    )
    neighbours = await _run(
        """
        MATCH (t:Thing {company_id: $c, id: $id})-[r]-(o:Thing {company_id: $c})
        RETURN o.id AS id, o.thing_type AS thing_type, o.title AS title,
               o.status AS status, type(r) AS type,
               startNode(r).id = $id AS outgoing
        """,
        c=company_id,
        id=thing_id,
    )
    return {"thing": rows[0], "events": events, "neighbours": neighbours}


async def things_missing_link(
    company_id: str, thing_type: str, rel_type: str, limit: int = 200
) -> list[dict]:
    """Things of a type with no in/out relationship of `rel_type` (watcher primitive)."""
    rel = _rel(rel_type)
    return await _run(
        f"""
        MATCH (t:Thing {{company_id: $c, thing_type: $tt}})
        WHERE NOT (t)-[:{rel}]-()
        RETURN t.id AS id, t.thing_type AS thing_type, t.title AS title,
               t.status AS status, t.last_activity AS last_activity
        LIMIT $limit
        """,
        c=company_id,
        tt=thing_type,
        limit=limit,
    )


async def stalled_things(
    company_id: str, stall_days: int, terminal_statuses: list[str], min_events: int = 2, limit: int = 200
) -> list[dict]:
    """Things with real prior activity (>= min_events mirrored events) whose
    last_activity has gone quiet for stall_days, and whose status isn't one
    of a small generic terminal-state vocabulary (watcher primitive, no
    profile input — company_id and pure timing/topology only)."""
    return await _run(
        """
        MATCH (t:Thing {company_id: $c})
        WHERE t.last_activity IS NOT NULL
          AND datetime(t.last_activity) < datetime() - duration({days: $stall_days})
          AND (t.status IS NULL OR NOT toLower(t.status) IN $terminal)
          AND size([(e:Event)-[:ABOUT]->(t) | e]) >= $min_events
        RETURN t.id AS id, t.thing_type AS thing_type, t.title AS title,
               t.status AS status, t.last_activity AS last_activity
        LIMIT $limit
        """,
        c=company_id, stall_days=stall_days,
        terminal=[s.lower() for s in terminal_statuses], min_events=min_events, limit=limit,
    )


async def single_event_aging_things(company_id: str, min_age_days: int, limit: int = 200) -> list[dict]:
    """Things created once and never touched again — a commitment declared
    but never followed up (watcher primitive; distinct from stalled_things,
    which requires having SEEN activity before going quiet)."""
    return await _run(
        """
        MATCH (t:Thing {company_id: $c})
        WHERE t.last_activity IS NOT NULL
          AND datetime(t.last_activity) < datetime() - duration({days: $min_age_days})
          AND size([(e:Event)-[:ABOUT]->(t) | e]) = 1
        RETURN t.id AS id, t.thing_type AS thing_type, t.title AS title,
               t.status AS status, t.last_activity AS last_activity
        LIMIT $limit
        """,
        c=company_id, min_age_days=min_age_days, limit=limit,
    )


async def hotspot_things(company_id: str, min_mentions: int = 3, limit: int = 200) -> list[dict]:
    """Things referenced by many others (MENTIONS/SAME_AS) but touched by NO
    relationship of any other type — lots of chatter, zero resolution
    activity. MENTIONS/SAME_AS are the engine's own canonical link names
    (every shipped profile uses them as-is), checked generically here rather
    than reading profile.links — a watcher primitive, not a profile lookup.
    """
    return await _run(
        """
        MATCH (t:Thing {company_id: $c})
        MATCH (other:Thing {company_id: $c})-[m:MENTIONS|SAME_AS]-(t)
        WITH t, count(DISTINCT other) AS mention_count
        WHERE mention_count >= $min_mentions
          AND NOT exists {
              MATCH (t)-[r]-()
              WHERE NOT type(r) IN ['MENTIONS', 'SAME_AS']
          }
        RETURN t.id AS id, t.thing_type AS thing_type, t.title AS title,
               t.status AS status, t.last_activity AS last_activity, mention_count
        LIMIT $limit
        """,
        c=company_id, min_mentions=min_mentions, limit=limit,
    )


async def similar_events(
    company_id: str, event_id: str, limit: int = 25
) -> list[dict]:
    """Candidate events near this one in embedding space (for resolve())."""
    emb = await event_embedding(company_id, event_id)
    if emb is None:
        return []
    rows = await vector_search(company_id, emb, limit=limit + 1)
    return [r for r in rows if r["event_id"] != event_id][:limit]


async def count_nodes(company_id: str | None = None) -> dict[str, int]:
    """Node counts (optionally per company) — used by proofs and health."""
    if company_id:
        rows = await _run(
            """
            MATCH (n) WHERE n.company_id = $c
            RETURN labels(n)[0] AS label, count(n) AS n
            """,
            c=company_id,
        )
    else:
        rows = await _run("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS n")
    out = {r["label"]: r["n"] for r in rows}
    out["total"] = sum(out.values())
    return out


async def wipe_company(company_id: str) -> int:
    """Delete every node for a company (tests / reseeds). Returns nodes removed."""
    rows = await _run(
        """
        MATCH (n) WHERE n.company_id = $c
        DETACH DELETE n
        RETURN count(n) AS n
        """,
        c=company_id,
    )
    return rows[0]["n"] if rows else 0
