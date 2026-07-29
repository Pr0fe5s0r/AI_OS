from __future__ import annotations

import hashlib
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import graph
from packages.shared.schema import Scope

# ---------------------------------------------------------------------------
# SELF-ORGANISING MEMORY.
#
# A background pass reshapes the store between queries, so the graph is not a
# fixed picture of what was written but a changing one that reflects what the
# store has worked out. Four operations, run in this order because each feeds
# the next:
#
#   dedupe   exact duplicates collapse into one node
#   merge    nodes that say the same thing become a summary node
#   decay    archived nodes lose importance on a half-life, then are dropped
#   promote  a node's stage is recomputed from its degree and history
#
# THE CONSEQUENCE, STATED PLAINLY: merging replaces passages with text a model
# wrote. A retrieval can therefore return a sentence that appears in no
# document. `node_type` marks those and `merged_from` records exactly which
# passages produced them, so the claim can always be traced back to evidence —
# a summary that cannot name its sources is an assertion, not a memory.
#
# The uploaded document is not touched. Passages are derived from
# `kb_items.body`, so rebuilding regenerates them from source whatever this
# pass did.
# ---------------------------------------------------------------------------

# Above this cosine, two nodes are treated as saying the same thing.
#
# 0.88 is the reference implementation's constant, kept as the default. It is
# NOT universal: a cosine threshold is a property of the embedding model, not
# of the idea of similarity. On Qwen3-Embedding-8B the closest pair in a real
# 28-passage specification scores 0.871, so at 0.88 nothing ever merges and the
# store looks broken rather than conservative. The same mistake once left two
# thirds of the neighbour graph unconnected with a borrowed 0.45 floor.
#
# So it is configurable, every run records the value it used and how many pairs
# cleared it, and the default stays high — merging is destructive to the
# retrievable surface, and a threshold that folds together merely-related
# passages erases distinctions the author made on purpose.
MERGE_THRESHOLD = float(os.getenv("CONSOLIDATION_MERGE_THRESHOLD", "0.88"))
# Half-life of an archived node's importance.
DECAY_HALF_LIFE_HOURS = 48.0
# Below this, an archived node stops being worth keeping.
DROP_FLOOR = 0.05
# A merge of more than this many nodes is a sign the threshold is wrong for
# this collection, not a genuine cluster of identical statements.
MAX_MERGE_GROUP = 8
# Stage 4 is for nodes that have survived enough passes to look settled.
LONG_TERM_CYCLES = 3
HUB_DEGREE = 4


@dataclass(slots=True)
class Outcome:
    """What one pass actually did. Counts, not a bare 'ok'."""

    merged: int = 0
    deduped: int = 0
    decayed: int = 0
    dropped: int = 0
    promoted: int = 0
    duration_ms: int = 0
    # The threshold this pass used, and the closest pair it saw. Together they
    # answer "why did nothing merge?" without anyone having to guess.
    threshold: float = MERGE_THRESHOLD
    closest_pair: float = 0.0
    candidates: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def text_hash(body: str) -> str:
    """Whitespace-insensitive, so two copies that differ only in wrapping are
    still one duplicate."""
    return hashlib.sha256(" ".join(body.split()).encode("utf-8")).hexdigest()[:32]


def summary_id(chunk_ids: list[str]) -> str:
    """Deterministic from the members, so the same group merged twice is the
    same node rather than an ever-growing pile of near-identical summaries."""
    seed = "\x1f".join(sorted(chunk_ids))
    return "sum-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:28]


# ------------------------------- 1. dedupe -------------------------------


async def dedupe(session: AsyncSession, scope: Scope) -> int:
    """Collapse exact content matches, keeping the earliest.

    Cheaper and surer than similarity: identical text needs no threshold and
    no judgement. Runs first so merging never wastes a model call comparing a
    passage with its own copy.
    """
    await session.execute(
        text(
            """
            UPDATE kb_chunks SET text_hash = encode(sha256(
                convert_to(regexp_replace(btrim(text), '\\s+', ' ', 'g'), 'UTF8')), 'hex')
            WHERE workspace_id = :w AND text_hash IS NULL
            """
        ),
        {"w": scope.workspace_id},
    )
    rows = (
        await session.execute(
            text(
                """
                UPDATE kb_chunks victim
                SET archived_at = now()
                FROM (
                    SELECT chunk_id, text_hash,
                           row_number() OVER (PARTITION BY text_hash ORDER BY created_at, chunk_id) AS rank
                    FROM kb_chunks
                    WHERE workspace_id = :w AND archived_at IS NULL
                      AND (CAST(:c AS text) IS NULL OR collection_id = CAST(:c AS text))
                ) ranked
                WHERE victim.chunk_id = ranked.chunk_id AND ranked.rank > 1
                RETURNING victim.chunk_id
                """
            ),
            {"w": scope.workspace_id, "c": scope.collection_id},
        )
    ).all()
    for row in rows:
        await graph.archive_chunk(scope, row.chunk_id)
    return len(rows)


# -------------------------------- 2. merge --------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def find_groups(
    points: list[dict[str, Any]], threshold: float = MERGE_THRESHOLD
) -> list[list[str]]:
    """Clusters of nodes that say the same thing, by transitive closure.

    Union-find rather than pairwise: if A matches B and B matches C, all three
    are one statement and must become one node. Merging them pairwise would
    leave a summary of a summary, which loses provenance a level at a time.
    """
    parent: dict[str, str] = {p["id"]: p["id"] for p in points}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(points):
        for b in points[i + 1 :]:
            if _cosine(a["embedding"], b["embedding"]) >= threshold:
                union(a["id"], b["id"])

    groups: dict[str, list[str]] = {}
    for p in points:
        groups.setdefault(find(p["id"]), []).append(p["id"])
    # A runaway group means the threshold is wrong for this collection, not
    # that fifty passages are identical. Leaving it alone is the safe read.
    return [g for g in groups.values() if 2 <= len(g) <= MAX_MERGE_GROUP]


_SUMMARY_PROMPT = (
    "These passages all state the same thing. Write ONE passage that preserves "
    "every distinct fact, figure and qualifier across them. Do not add anything "
    "that is not in the passages, do not editorialise, and do not use phrases "
    "like 'the passages state'. Reply with the passage only.\n\n"
)


def _fallback_summary(texts: list[str]) -> str:
    """Used when no model is reachable: keep the longest member verbatim.

    Inventing a summary is not an option and neither is failing the whole pass,
    so the fullest of the originals stands in. It is real text from a real
    document, which is the safer of the two wrong answers.
    """
    return max(texts, key=len)


async def merge(session: AsyncSession, scope: Scope) -> tuple[int, float, int]:
    """Fold groups of equivalent nodes into one summary node each.

    Returns the number merged, the closest pair seen, and how many pairs
    cleared the threshold — so a pass that merged nothing can say whether the
    data was too dissimilar or the threshold too high.
    """
    points = await graph.collection_chunk_vectors(scope, limit=400, live_only=True)
    closest, above = _pair_stats(points)
    groups = find_groups(points)
    if not groups:
        return 0, closest, above

    from packages.core.llm import chat, embed

    merged = 0
    for group in groups:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT chunk_id, heading, text, importance, item_id, collection_id
                    FROM kb_chunks
                    WHERE workspace_id = :w AND chunk_id = ANY(:ids) AND archived_at IS NULL
                    ORDER BY length(text) DESC
                    """
                ),
                {"w": scope.workspace_id, "ids": group},
            )
        ).all()
        if len(rows) < 2:
            continue

        bodies = [r.text for r in rows]
        try:
            body = chat(
                [{"role": "user", "content": _SUMMARY_PROMPT + "\n\n---\n\n".join(bodies)}]
            ).strip()
        except Exception:
            body = ""
        if not body:
            body = _fallback_summary(bodies)

        node_id = summary_id(group)
        heading = rows[0].heading
        # A summary is at least as important as its most important member: it
        # now stands for all of them.
        importance = min(1.0, max(float(r.importance) for r in rows) + 0.1)

        await session.execute(
            text(
                """
                INSERT INTO kb_chunks
                    (chunk_id, workspace_id, collection_id, item_id, version, ordinal,
                     heading, text, node_type, importance, stage, merged_from, text_hash)
                VALUES (:cid, :w, :c, NULL, NULL, 0, :heading, :text,
                        'summary', :importance, 3, CAST(:sources AS jsonb), :hash)
                ON CONFLICT (chunk_id) DO UPDATE
                    SET text = EXCLUDED.text,
                        importance = EXCLUDED.importance,
                        merged_from = EXCLUDED.merged_from,
                        archived_at = NULL
                """
            ),
            {
                "cid": node_id, "w": scope.workspace_id, "c": rows[0].collection_id,
                "heading": heading, "text": body, "importance": importance,
                "sources": _json(group), "hash": text_hash(body),
            },
        )
        await session.execute(
            text(
                "UPDATE kb_chunks SET archived_at = now() "
                "WHERE workspace_id = :w AND chunk_id = ANY(:ids)"
            ),
            {"w": scope.workspace_id, "ids": group},
        )

        vector = embed(f"{heading}\n\n{body}" if heading else body)
        await graph.upsert_summary(
            scope,
            chunk_id=node_id,
            heading=heading,
            embedding=vector,
            sources=group,
        )
        for cid in group:
            await graph.archive_chunk(scope, cid)
        merged += 1
    return merged, closest, above


def _pair_stats(points: list[dict[str, Any]]) -> tuple[float, int]:
    """The closest pair in the collection, and how many clear the threshold."""
    closest, above = 0.0, 0
    for i, a in enumerate(points):
        for b in points[i + 1 :]:
            sim = _cosine(a["embedding"], b["embedding"])
            closest = max(closest, sim)
            if sim >= MERGE_THRESHOLD:
                above += 1
    return round(closest, 4), above


def _json(value: Any) -> str:
    import json

    return json.dumps(value)


# -------------------------------- 3. decay --------------------------------


async def decay(session: AsyncSession, scope: Scope) -> tuple[int, int]:
    """Archived nodes lose importance on a half-life, then are dropped.

    Recomputed from `archived_at` rather than multiplied down each pass, so the
    result does not depend on how many times this ran — a pass that was missed,
    or one that ran twice, gives the same answer.
    """
    decayed = (
        await session.execute(
            text(
                f"""
                UPDATE kb_chunks
                SET importance = GREATEST(0.0, LEAST(1.0,
                        0.5 * power(0.5, EXTRACT(EPOCH FROM (now() - archived_at))
                                         / 3600.0 / {DECAY_HALF_LIFE_HOURS})))
                WHERE workspace_id = :w AND archived_at IS NOT NULL
                  AND (CAST(:c AS text) IS NULL OR collection_id = CAST(:c AS text))
                RETURNING chunk_id
                """  # noqa: S608 - half-life is a module constant, not input
            ),
            {"w": scope.workspace_id, "c": scope.collection_id},
        )
    ).all()

    gone = (
        await session.execute(
            text(
                """
                DELETE FROM kb_chunks
                WHERE workspace_id = :w AND archived_at IS NOT NULL
                  AND importance < :floor
                  AND (CAST(:c AS text) IS NULL OR collection_id = CAST(:c AS text))
                RETURNING chunk_id
                """
            ),
            {"w": scope.workspace_id, "c": scope.collection_id, "floor": DROP_FLOOR},
        )
    ).all()
    for row in gone:
        await graph.delete_chunk(scope, row.chunk_id)
    return len(decayed), len(gone)


# ------------------------------- 4. promote -------------------------------


async def promote(session: AsyncSession, scope: Scope, degrees: dict[str, int]) -> int:
    """Recompute each live node's stage.

      1  raw passage, nothing near it
      2  connected — it has neighbours in embedding space
      3  a working-memory hub: a summary, or a passage many others sit around
      4  long-term: a hub that has survived several passes unchanged

    Derived every pass rather than incremented, so a node that loses its
    neighbours falls back down instead of keeping a rank it no longer earns.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT chunk_id, node_type, cycles, stage FROM kb_chunks
                WHERE workspace_id = :w AND archived_at IS NULL
                  AND (CAST(:c AS text) IS NULL OR collection_id = CAST(:c AS text))
                """
            ),
            {"w": scope.workspace_id, "c": scope.collection_id},
        )
    ).all()

    changed = 0
    for row in rows:
        degree = degrees.get(row.chunk_id, 0)
        if row.node_type == "summary" or degree >= HUB_DEGREE:
            stage = 4 if row.cycles >= LONG_TERM_CYCLES else 3
        elif degree > 0:
            stage = 2
        else:
            stage = 1
        await session.execute(
            text(
                "UPDATE kb_chunks SET stage = :stage, cycles = cycles + 1 "
                "WHERE workspace_id = :w AND chunk_id = :cid"
            ),
            {"stage": stage, "w": scope.workspace_id, "cid": row.chunk_id},
        )
        if stage != row.stage:
            changed += 1
    return changed


# ------------------------------ the whole pass ------------------------------


async def run_once(session: AsyncSession, scope: Scope) -> Outcome:
    """One consolidation pass over one collection.

    Order matters: dedupe first so merging never compares a passage with its
    own copy, merge before decay so freshly archived members start decaying
    immediately, promote last so stages reflect the shape that resulted.
    """
    started = time.perf_counter()
    outcome = Outcome()
    outcome.deduped = await dedupe(session, scope)
    await session.commit()

    outcome.merged, outcome.closest_pair, outcome.candidates = await merge(session, scope)
    await session.commit()

    outcome.decayed, outcome.dropped = await decay(session, scope)
    await session.commit()

    live = await graph.collection_chunk_vectors(scope, limit=400, live_only=True)
    degrees = await _degrees(live)
    outcome.promoted = await promote(session, scope, degrees)
    outcome.duration_ms = int((time.perf_counter() - started) * 1000)

    await session.execute(
        text(
            """
            INSERT INTO consolidation_runs
                (workspace_id, collection_id, merged, deduped, decayed, dropped,
                 promoted, duration_ms)
            VALUES (:w, :c, :merged, :deduped, :decayed, :dropped, :promoted, :ms)
            """
        ),
        {
            "w": scope.workspace_id, "c": scope.collection_id,
            "merged": outcome.merged, "deduped": outcome.deduped,
            "decayed": outcome.decayed, "dropped": outcome.dropped,
            "promoted": outcome.promoted, "ms": outcome.duration_ms,
        },
    )
    await session.commit()
    return outcome


async def _degrees(points: list[dict[str, Any]]) -> dict[str, int]:
    """Neighbour counts, from the same k-NN rule the graph view draws."""
    from packages.core.neighbours import knn_edges

    edges, _ = knn_edges(points)
    degrees: dict[str, int] = {}
    for edge in edges:
        degrees[edge["src"]] = degrees.get(edge["src"], 0) + 1
        degrees[edge["dst"]] = degrees.get(edge["dst"], 0) + 1
    return degrees


async def recent_runs(
    session: AsyncSession, scope: Scope, limit: int = 20
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT id, collection_id, merged, deduped, decayed, dropped,
                       promoted, duration_ms, error, created_at
                FROM consolidation_runs
                WHERE workspace_id = :w AND (CAST(:c AS text) IS NULL OR collection_id = CAST(:c AS text))
                ORDER BY created_at DESC LIMIT :limit
                """
            ),
            {"w": scope.workspace_id, "c": scope.collection_id, "limit": limit},
        )
    ).all()
    return [
        {
            "id": r.id, "collection_id": r.collection_id, "merged": r.merged,
            "deduped": r.deduped, "decayed": r.decayed, "dropped": r.dropped,
            "promoted": r.promoted, "duration_ms": r.duration_ms, "error": r.error,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


async def touch(session: AsyncSession, scope: Scope, chunk_ids: list[str]) -> None:
    """Retrieval raises a node's importance.

    What gets used survives; what is never used decays. Bounded at 1.0 so a
    node cannot become permanent by being asked for repeatedly.
    """
    if not chunk_ids:
        return
    await session.execute(
        text(
            """
            UPDATE kb_chunks
            SET access_count = access_count + 1,
                last_accessed_at = :now,
                importance = LEAST(1.0, importance + 0.02)
            WHERE workspace_id = :w AND chunk_id = ANY(:ids)
            """
        ),
        {"w": scope.workspace_id, "ids": chunk_ids, "now": datetime.now(UTC)},
    )


__all__ = [
    "DECAY_HALF_LIFE_HOURS",
    "DROP_FLOOR",
    "MERGE_THRESHOLD",
    "Outcome",
    "decay",
    "dedupe",
    "find_groups",
    "merge",
    "promote",
    "recent_runs",
    "run_once",
    "summary_id",
    "text_hash",
    "touch",
]
