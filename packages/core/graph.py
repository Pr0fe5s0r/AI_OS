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


# Labels this store stopped using when it became a knowledge base. Their nodes
# are gone; their indexes were not, because dropping an index is not something
# a schema migration on Postgres can reach into Neo4j and do.
_RETIRED_LABELS = ("Event", "Thing")


async def drop_retired_indexes() -> list[str]:
    """Remove indexes and constraints for labels nothing writes any more.

    A vector index on a label with no nodes is not harmless: it is 1536
    dimensions of structure the database keeps ready for writes that will never
    come, and it shows up in every listing as though it meant something.

    Guarded on the label being genuinely empty. If a node of that label exists
    the index stays, because an index dropped out from under live data is a
    silent full scan rather than an error — and this runs on every boot.
    """
    dropped: list[str] = []
    for label in _RETIRED_LABELS:
        rows = await _run(f"MATCH (n:{label}) RETURN count(n) AS n")
        if rows and rows[0]["n"]:
            continue

        constraints = await _run(
            "SHOW CONSTRAINTS YIELD name, labelsOrTypes RETURN name, labelsOrTypes AS labels"
        )
        for entry in constraints:
            if label in (entry.get("labels") or []):
                await _run(f"DROP CONSTRAINT {entry['name']} IF EXISTS")
                dropped.append(entry["name"])

        # After the constraints, or dropping a constraint would take its
        # backing index with it and this would try to drop it twice.
        indexes = await _run(
            "SHOW INDEXES YIELD name, labelsOrTypes RETURN name, labelsOrTypes AS labels"
        )
        for entry in indexes:
            if label in (entry.get("labels") or []):
                await _run(f"DROP INDEX {entry['name']} IF EXISTS")
                dropped.append(entry["name"])
    return dropped


async def bootstrap() -> None:
    """Constraints and indexes, created idempotently. Safe on every boot."""
    await _move_vector_index_to_chunks()
    await drop_retired_indexes()
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
    # Index summaries (card / section_summary) are a navigation layer, not
    # answer content — they are model-written and must never be returned as a
    # citation. Merge summaries ('summary') stay recallable: they REPLACE the
    # archived passages they came from, so excluding them would lose content.
    clause += " AND NOT coalesce(node.node_type, 'fact') IN ['card', 'section_summary']"
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
            c.status        = $status,
            c.node_type     = 'fact',
            c.stage         = 1,
            c.text_hash     = row.text_hash
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


async def vectors_by_text_hash(scope: Scope, item_id: str) -> dict[str, list[float]]:
    """The vectors this document already has, keyed by the text that produced them.

    Editing one clause of a specification re-versions the whole document, and
    the whole document was then re-embedded: 182 passages sent to the provider
    to learn 181 things it had already told us. This is what makes the second
    ingest of a document cost only what actually changed.

    Keyed by content, not by chunk_id, because chunk_id carries the version and
    the ordinal — both of which move when a paragraph is inserted near the top,
    even though every passage after it is character-for-character the same.
    """
    clause, params = _scope_clause("c", scope)
    rows = await _run(
        f"""
        MATCH (c:Chunk {{item_id: $item_id}})
        WHERE {clause} AND c.text_hash IS NOT NULL AND c.embedding IS NOT NULL
        RETURN c.text_hash AS text_hash, c.embedding AS embedding
        """,
        item_id=item_id,
        **params,
    )
    return {row["text_hash"]: list(row["embedding"]) for row in rows}


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
        # Every Chunk has status from the moment it is written. `archived` is
        # intentionally absent until consolidation archives a node, so asking
        # for c.archived before that has ever happened makes Neo4j emit an
        # UnknownPropertyKeyWarning on otherwise healthy collections.
        clause += " AND c.status = $active"
        params["active"] = str(Lifecycle.ACTIVE)

    # Which documents are here, and how big each is. One cheap grouping, and it
    # is what makes the sampling below fair.
    counts = await _run(
        f"""
        MATCH (c:Chunk) WHERE {clause} AND c.embedding IS NOT NULL
        RETURN c.item_id AS item_id, count(*) AS n
        ORDER BY n DESC
        """,
        **params,
    )
    items = [(r["item_id"], int(r["n"])) for r in counts if r["item_id"]]
    if not items:
        return []

    # A SHARE EACH, not an alphabetical prefix.
    #
    # This was `ORDER BY c.item_id, c.ordinal LIMIT 400`, which hands the whole
    # budget to whichever documents happen to sort first. Measured on a
    # three-document collection: bitcoin (~40 passages) and a Harry Potter
    # collection (11,460) between them filled all 400 slots, and the Server
    # Requirements document — sorting last by id — contributed ZERO points. The
    # graph reported "2 documents" for a collection holding three, and the
    # missing one looked like it had never been indexed.
    #
    # Every document now gets an equal share, and any budget left over by
    # documents smaller than their share is handed back to the larger ones, so
    # the picture stays full without starving anybody.
    share = max(1, limit // len(items))
    spare = limit - sum(min(n, share) for _, n in items)
    rows: list[dict] = []
    for item_id, n in sorted(items, key=lambda pair: pair[1]):
        take = min(n, share)
        if spare > 0 and n > take:
            extra = min(spare, n - take)
            take += extra
            spare -= extra
        rows.extend(
            await _run(
                f"""
                MATCH (c:Chunk) WHERE {clause} AND c.embedding IS NOT NULL
                  AND c.item_id = $item_id
                RETURN c.chunk_id AS id, c.item_id AS item_id, c.ordinal AS ordinal,
                       c.heading AS heading, c.title AS title, c.embedding AS embedding,
                       coalesce(properties(c)['node_type'], 'fact') AS node_type,
                       coalesce(properties(c)['stage'], 1) AS stage
                ORDER BY c.ordinal
                LIMIT $take
                """,
                item_id=item_id,
                take=take,
                **params,
            )
        )
    return rows[:limit]


async def replace_near_edges(scope: Scope, edges: list[dict]) -> int:
    """Persist the similarity neighbour graph as :NEAR relationships.

    The graph view computes these in Python on every request; a traversal tool
    cannot afford that, so consolidation writes them down and a hop becomes a
    single relationship lookup. Rebuilt, not merged: an edge that no longer
    clears the floor has to disappear, so the collection's NEAR edges are
    dropped and the current set written in their place.

    Stored once per unordered pair — the query that reads them is
    direction-agnostic — carrying the cosine that produced the edge. Each dict
    carries src, dst and similarity, exactly as ``neighbours.knn_edges`` returns.
    """
    clause, params = _scope_clause("c", scope)
    await _run(
        f"MATCH (c:Chunk)-[r:NEAR]->(:Chunk) WHERE {clause} DELETE r",
        **params,
    )
    if not edges:
        return 0
    rows = await _run(
        """
        UNWIND $edges AS e
        MATCH (a:Chunk {chunk_id: e.src})
        MATCH (b:Chunk {chunk_id: e.dst})
        MERGE (a)-[r:NEAR]->(b)
        SET r.similarity = e.similarity
        RETURN count(r) AS n
        """,
        edges=[
            {"src": e["src"], "dst": e["dst"], "similarity": e["similarity"]} for e in edges
        ],
    )
    return rows[0]["n"] if rows else 0


async def chunk_neighbours(scope: Scope, chunk_id: str, limit: int = 10) -> list[dict]:
    """The passages related to this one — the store's own graph links, either way.

    This is the traversal primitive. Given a passage, it returns what sits next
    to it, so an agent — ours, or a caller's own LLM through the SDK — can walk
    from one passage to related material a fresh search might not reach, a hop at
    a time.

    Two kinds of edge, and an AUTHORED one always beats a merely-similar one:
      :RELATED  a typed judgement (elaborates/defines/supports/contradicts/
                precedes) written by the labelling pass — a reason two passages
                belong together, which cosine cannot reconstruct.
      :NEAR     cosine similarity, computed by consolidation.
    Both are matched in both directions (stored once per pair). A neighbour
    reachable by both is returned once, as its typed relation. `relation` is the
    edge kind ("near" when only similarity links them); results are ordered
    authored-first, then by weight. Archived neighbours are excluded.
    """
    clause, params = _scope_clause("c", scope)
    # The FAR end of the hop is scoped too, and that is the point. Scoping only
    # the chunk you start from lets a single edge carry the answer into another
    # collection — a caller bound to one client would receive another client's
    # passage as a "neighbour", which is precisely the leak the binding exists
    # to make impossible. An edge is not permission to cross a boundary.
    neighbour_clause, _ = _scope_clause("n", scope)
    return await _run(
        f"""
        MATCH (c:Chunk {{chunk_id: $chunk_id}}) WHERE {clause}
        MATCH (c)-[r:NEAR|RELATED]-(n:Chunk)
        WHERE {neighbour_clause}
          AND n.status = $active
          AND NOT (type(r) = 'RELATED' AND coalesce(r.relation, 'none') = 'none')
        WITH n, collect(r) AS rels
        WITH n,
             head([x IN rels WHERE type(x) = 'RELATED']) AS typed,
             head([x IN rels WHERE type(x) = 'NEAR']) AS near
        WITH n, typed,
             CASE WHEN typed IS NOT NULL THEN typed.relation ELSE 'near' END AS relation,
             CASE WHEN typed IS NOT NULL THEN coalesce(typed.confidence, 0.6)
                  ELSE coalesce(near.similarity, 0.0) END AS weight
        RETURN n.chunk_id AS chunk_id, n.item_id AS item_id,
               n.heading AS heading, n.title AS title,
               coalesce(properties(n)['node_type'], 'fact') AS node_type,
               relation AS relation, weight AS similarity,
               (typed IS NOT NULL) AS typed
        ORDER BY typed DESC, weight DESC
        LIMIT $limit
        """,
        chunk_id=chunk_id,
        active=str(Lifecycle.ACTIVE),
        limit=limit,
        **params,
    )


async def unlabeled_near_pairs(scope: Scope, limit: int = 20) -> list[dict]:
    """NEAR pairs that have not been typed yet — the edge-labeller's worklist.

    One row per unordered pair (a.chunk_id < b.chunk_id), and only pairs with no
    :RELATED edge in either direction — so a pair is processed once, whatever the
    labeller decided (a real relation, or 'none').
    """
    clause, params = _scope_clause("a", scope)
    return await _run(
        f"""
        MATCH (a:Chunk)-[:NEAR]-(b:Chunk)
        WHERE {clause} AND a.chunk_id < b.chunk_id
          AND coalesce(a.node_type, 'fact') = 'fact'
          AND coalesce(b.node_type, 'fact') = 'fact'
          AND NOT (a)-[:RELATED]-(b)
        RETURN DISTINCT a.chunk_id AS src, b.chunk_id AS dst
        LIMIT $limit
        """,
        limit=limit,
        **params,
    )


async def link_chunk(
    scope: Scope, src: str, dst: str, relation: str, confidence: float
) -> None:
    """Write one authored typed edge between two passages (directional src→dst).

    A 'none' relation is still recorded — it marks the pair as processed so the
    labeller does not keep paying to re-decide a pair it already judged
    unrelated. The traversal filters 'none' out.
    """
    await _run(
        """
        MATCH (a:Chunk {chunk_id: $src})
        MATCH (b:Chunk {chunk_id: $dst})
        MERGE (a)-[r:RELATED]->(b)
        SET r.relation = $relation, r.confidence = $confidence
        """,
        src=src,
        dst=dst,
        relation=relation,
        confidence=confidence,
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


async def delete_item(scope: Scope, item_id: str) -> dict[str, int]:
    """Remove a document from the graph: its passages, then the document node.

    Passages first, because the Item node is what the scope clause is anchored
    on for some callers and an orphaned Chunk is worse than an orphaned Item —
    a Chunk still carries an embedding, so it still answers questions, which is
    exactly what a deleted document must never do.

    DETACH so the relationships go with them. Scoped like every query here:
    a workspace can only delete inside itself.
    """
    chunk_clause, chunk_params = _scope_clause("c", scope)
    passages = await _run(
        f"MATCH (c:Chunk {{item_id: $item_id}}) WHERE {chunk_clause} "
        "WITH c, count(c) AS _ DETACH DELETE c RETURN count(*) AS gone",
        item_id=item_id,
        **chunk_params,
    )
    item_clause, item_params = _scope_clause("i", scope)
    items = await _run(
        f"MATCH (i:Item {{item_id: $item_id}}) WHERE {item_clause} "
        "DETACH DELETE i RETURN count(*) AS gone",
        item_id=item_id,
        **item_params,
    )
    return {
        "chunks": int(passages[0]["gone"]) if passages else 0,
        "items": int(items[0]["gone"]) if items else 0,
    }


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
    # Both ends, for the same reason as chunk_neighbours: a DERIVED_FROM edge
    # must not hand back a source passage from another collection.
    source_clause, _ = _scope_clause("s", scope)
    return await _run(
        f"""
        MATCH (c:Chunk {{chunk_id: $chunk_id}})-[:DERIVED_FROM]->(s:Chunk)
        WHERE {clause} AND {source_clause}
        RETURN s.chunk_id AS chunk_id, s.heading AS heading,
               s.item_id AS item_id, properties(s)['archived'] AS archived
        """,
        chunk_id=chunk_id,
        **params,
    )


# ------------------------------ index summaries ------------------------------
#
# A card (per document) or section_summary (per section / probe-mapped cluster)
# is a :Chunk with an embedding — so it is traversable and could be searched —
# joined to the live passages it stands for by :SUMMARIZES. Unlike a merge
# summary (DERIVED_FROM), the covered passages are NOT archived: the summary is
# a navigation layer laid OVER the facts, not a replacement for them. Text lives
# in Postgres; this stores only the node, its vector, and the edges.

INDEX_SUMMARY_TYPES = ("card", "section_summary")


async def upsert_index_summary(
    scope: Scope,
    chunk_id: str,
    heading: str,
    embedding: list[float],
    covers: list[str],
    kind: str,
    item_id: str | None = None,
) -> None:
    """Create/refresh a summary node and its SUMMARIZES edges to live chunks.

    ``kind`` is 'card' or 'section_summary'. The covered chunks stay live and
    retrievable — this only lays a navigable edge over them. Rebuilt each time
    it is generated: existing SUMMARIZES edges for this summary are dropped so a
    regenerated summary cannot keep pointing at passages it no longer covers.
    """
    if kind not in INDEX_SUMMARY_TYPES:
        raise ValueError(f"not an index-summary kind: {kind!r}")
    await _run(
        """
        MERGE (c:Chunk {chunk_id: $chunk_id})
        SET c.workspace_id  = $workspace,
            c.collection_id = $collection,
            c.item_id       = $item_id,
            c.heading       = $heading,
            c.title         = $heading,
            c.node_type     = $kind,
            c.status        = $status,
            c.ordinal       = 0,
            c.archived      = NULL
        """,
        chunk_id=chunk_id,
        workspace=scope.workspace_id,
        collection=scope.collection_id,
        item_id=item_id,
        heading=heading,
        kind=kind,
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
    # Rebuild the coverage edges: drop this summary's old SUMMARIZES, write the
    # current set. A stale edge is worse than none.
    await _run(
        "MATCH (c:Chunk {chunk_id: $chunk_id})-[r:SUMMARIZES]->() DELETE r",
        chunk_id=chunk_id,
    )
    if covers:
        await _run(
            """
            MATCH (c:Chunk {chunk_id: $chunk_id})
            UNWIND $covers AS target
            MATCH (t:Chunk {chunk_id: target})
            MERGE (c)-[:SUMMARIZES]->(t)
            """,
            chunk_id=chunk_id,
            covers=covers,
        )


async def unmapped_chunks(scope: Scope, limit: int = 50) -> list[dict]:
    """Live fact passages that no summary covers yet — the mapper's worklist.

    A chunk is "mapped" once a :SUMMARIZES edge points at it. This is the
    source of truth for coverage, so the count the UI shows and the work the
    mapper does can never disagree.
    """
    clause, params = _scope_clause("c", scope)
    return await _run(
        f"""
        MATCH (c:Chunk) WHERE {clause}
          AND c.status = $active
          AND coalesce(c.node_type, 'fact') = 'fact'
          AND NOT (:Chunk)-[:SUMMARIZES]->(c)
        RETURN c.chunk_id AS chunk_id, c.item_id AS item_id,
               c.heading AS heading, c.title AS title
        ORDER BY c.item_id, c.ordinal
        LIMIT $limit
        """,
        active=str(Lifecycle.ACTIVE),
        limit=limit,
        **params,
    )


async def mapping_counts(scope: Scope) -> dict:
    """How many live fact passages are mapped vs not — the header stat."""
    clause, params = _scope_clause("c", scope)
    rows = await _run(
        f"""
        MATCH (c:Chunk) WHERE {clause}
          AND c.status = $active
          AND coalesce(c.node_type, 'fact') = 'fact'
        RETURN count(c) AS total,
               count(CASE WHEN (:Chunk)-[:SUMMARIZES]->(c) THEN 1 END) AS mapped
        """,
        active=str(Lifecycle.ACTIVE),
        **params,
    )
    row = rows[0] if rows else {"total": 0, "mapped": 0}
    total, mapped = int(row["total"]), int(row["mapped"])
    return {"total": total, "mapped": mapped, "unmapped": total - mapped}


async def summary_coverage(scope: Scope) -> list[dict]:
    """Every summary node with the count of passages it connects — for the UI."""
    clause, params = _scope_clause("c", scope)
    return await _run(
        f"""
        MATCH (c:Chunk) WHERE {clause}
          AND coalesce(c.node_type, 'fact') IN ['card', 'section_summary']
        OPTIONAL MATCH (c)-[:SUMMARIZES]->(t:Chunk)
        RETURN c.chunk_id AS chunk_id, c.node_type AS node_type,
               c.item_id AS item_id, c.heading AS heading,
               count(t) AS covers
        ORDER BY c.node_type, covers DESC
        """,
        **params,
    )


async def has_near_edges(scope: Scope) -> bool:
    """Does this collection actually have a similarity graph to walk?

    :NEAR edges are written by consolidation, and consolidation is OFF by
    default. So on an ordinary deployment there are none — and the agent was
    still being handed a `neighbors` tool built to traverse them. Neo4j says so
    on every call ("the missing relationship type is: NEAR") and returns an
    empty result, which costs a full model round to learn nothing.

    One indexed lookup with LIMIT 1, so asking is far cheaper than the round it
    saves.
    """
    clause, params = _scope_clause("c", scope)
    rows = await _run(
        f"MATCH (c:Chunk)-[:NEAR]-(:Chunk) WHERE {clause} RETURN 1 AS ok LIMIT 1",
        **params,
    )
    return bool(rows)


async def documents_with_cards(scope: Scope) -> list[str]:
    """Which documents already have a card.

    The whole point of a backfill is not paying twice. Summarising a document
    is one model call per section plus one for the card — on a twelve-section
    circular that is thirteen calls, and the existing "summarize now" endpoint
    re-ran every one of them for every document each time it was pressed. On a
    hundred-document store that is roughly 1,300 calls to regenerate summaries
    that were already correct.

    So the pass asks this first and enqueues only what is missing.
    """
    clause, params = _scope_clause("c", scope)
    rows = await _run(
        f"""
        MATCH (c:Chunk) WHERE {clause}
          AND coalesce(c.node_type, 'fact') = 'card'
          AND c.item_id IS NOT NULL
        RETURN DISTINCT c.item_id AS item_id
        """,
        **params,
    )
    return [r["item_id"] for r in rows if r.get("item_id")]


async def card_matches(scope: Scope, embedding: list[float], k: int = 20) -> list[dict]:
    """Rank DOCUMENTS by how well their card matches a question.

    The mirror image of ``vector_search``, which deliberately excludes cards
    because a model-written summary must never be returned as a citation. That
    exclusion is about ANSWERING. Choosing which document to open is a different
    job, and it is the one job a card is actually good at: two or three
    sentences saying what the whole file is about, which is what you want to
    compare a question against when you have hundreds of documents and no idea
    which one is relevant.

    Scored EXACTLY, not through the ANN index, and that is the whole trick.

    The obvious implementation — query the vector index and keep the rows that
    happen to be cards — was written first and measured at returning **nothing**
    on a real store. Cards are one node per document: 14 of 10,265 chunks, or
    0.14%. Over-fetching the top 800 neighbours and filtering to that 0.14%
    found zero cards, every time, because a question resembles the passages that
    answer it far more closely than a summary of the file they sit in. Routing
    silently fell back to passages alone and nothing looked broken.

    Filter-after-ANN cannot work when the target class is that rare. So this
    reads the cards — there are exactly as many as there are documents — and
    scores every one. Exact, no recall cliff, and cheap for the same reason it
    is necessary: the population is small by construction. A ten-thousand
    document store is ten thousand comparisons, which Neo4j does in milliseconds.

    Returns at most one row per document, each with the similarity that put it
    there, so the caller can report WHY a document was opened rather than
    presenting a routing decision as an oracle.
    """
    clause, params = _scope_clause("c", scope)
    return await _run(
        f"""
        MATCH (c:Chunk) WHERE {clause}
          AND coalesce(c.node_type, 'fact') = 'card'
          AND c.item_id IS NOT NULL
          AND c.embedding IS NOT NULL
        WITH c, vector.similarity.cosine(c.embedding, $embedding) AS similarity
        WHERE similarity IS NOT NULL
        RETURN c.item_id AS item_id,
               c.heading AS heading,
               similarity AS similarity
        ORDER BY similarity DESC
        LIMIT $k
        """,
        # ``k <= 0`` means "no limit" to every caller in this codebase, and it
        # means "return nothing" to Cypher. That collision silently killed the
        # entire card arm: MAX_DOCUMENTS defaulted to 0, the route limit became
        # 0, and `LIMIT 0` returned zero cards on every question — routing fell
        # back to passages and reported it, and nothing else looked wrong.
        #
        # Second time this convention has leaked into an index expression; the
        # first emptied the document list via documents[:0]. Translated here, at
        # the boundary, rather than trusting each caller to remember.
        k=k if k > 0 else 10_000,
        embedding=embedding,
        **params,
    )


async def chunk_summaries(scope: Scope, item_id: str) -> list[dict]:
    """A document's card + section summaries — the navigator's rich catalogue."""
    clause, params = _scope_clause("c", scope)
    return await _run(
        f"""
        MATCH (c:Chunk) WHERE {clause}
          AND c.item_id = $item_id
          AND coalesce(c.node_type, 'fact') IN ['card', 'section_summary']
        OPTIONAL MATCH (c)-[:SUMMARIZES]->(t:Chunk)
        RETURN c.chunk_id AS chunk_id, c.node_type AS node_type,
               c.heading AS heading, count(t) AS covers
        ORDER BY c.node_type DESC
        """,
        item_id=item_id,
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
