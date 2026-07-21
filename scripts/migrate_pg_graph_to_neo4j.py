#!/usr/bin/env python3
"""One-shot migration: legacy Postgres graph + pgvector embeddings -> Neo4j.

The v1 engine kept the knowledge graph in Postgres (`nodes`/`edges`, traversed
with recursive CTEs) and embeddings in pgvector (`event_embeddings`). v2 keeps
the graph + vectors in Neo4j. This job:

  1. copies `nodes`   -> (:Thing) (and an (:Event) mirror for event-backed nodes)
  2. copies `edges`   -> typed relationships (confidence = old weight)
  3. copies `event_embeddings` -> (:Event).embedding (native vector index)
  4. DROPs the three legacy tables

Idempotent and safe on fresh databases: if the legacy tables don't exist,
it reports "nothing to migrate" and exits 0.

Usage:
    python scripts/migrate_pg_graph_to_neo4j.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
from datetime import UTC, datetime

from sqlalchemy import text

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from packages.core import graph  # noqa: E402
from packages.core.db import Session  # noqa: E402

LEGACY_TABLES = ("nodes", "edges", "event_embeddings")


def _parse_ts(value) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.now(UTC)


async def _existing(session) -> set[str]:
    rows = await session.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    )
    return {r.tablename for r in rows} & set(LEGACY_TABLES)


async def main() -> int:
    await graph.bootstrap()
    async with Session() as session:
        present = await _existing(session)
        if not present:
            print("nothing to migrate: no legacy graph/embedding tables found")
            return 0

        moved_things = moved_events = moved_links = moved_vectors = 0

        if "nodes" in present:
            rows = await session.execute(
                text("SELECT id, company_id, type, label, source, metadata FROM nodes")
            )
            for r in rows:
                md = r.metadata if isinstance(r.metadata, dict) else {}
                await graph.upsert_thing(
                    company_id=r.company_id,
                    thing_id=r.id,
                    thing_type=r.type,
                    title=r.label,
                    status=md.get("state") or md.get("status"),
                    last_activity=_parse_ts(md.get("timestamp")) if md.get("timestamp") else None,
                )
                moved_things += 1
                if r.source:  # event-backed node -> also mirror the event + ABOUT
                    await graph.mirror_event(
                        company_id=r.company_id,
                        event_id=r.id,
                        event_time=_parse_ts(md.get("timestamp")),
                        source=r.source,
                    )
                    await graph.link_event_to_thing(r.company_id, r.id, r.id)
                    moved_events += 1

        if "edges" in present:
            rows = await session.execute(
                text("SELECT company_id, src_id, dst_id, type, weight, metadata FROM edges")
            )
            for r in rows:
                md = r.metadata if isinstance(r.metadata, dict) else {}
                await graph.link_things(
                    company_id=r.company_id,
                    src_id=r.src_id,
                    dst_id=r.dst_id,
                    rel_type=r.type,
                    confidence=float(r.weight),
                    method=md.get("method"),
                )
                moved_links += 1

        if "event_embeddings" in present and "nodes" in present:
            rows = await session.execute(
                text(
                    """
                    SELECT em.event_id, em.embedding::text AS vec, n.company_id
                    FROM event_embeddings em JOIN nodes n ON n.id = em.event_id
                    """
                )
            )
            for r in rows:
                vector = [float(x) for x in r.vec.strip("[]").split(",")]
                await graph.set_event_embedding(r.company_id, r.event_id, vector)
                moved_vectors += 1

        for table in ("edges", "nodes", "event_embeddings"):
            await session.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        await session.commit()

    await graph.close_driver()
    print(
        f"migrated: {moved_things} things, {moved_events} event mirrors, "
        f"{moved_links} links, {moved_vectors} embeddings; legacy tables dropped"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
