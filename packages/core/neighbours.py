from __future__ import annotations

import math
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import graph
from packages.core.classify import classes_for
from packages.core.projection import project
from packages.shared.schema import Scope

# ---------------------------------------------------------------------------
# THE NEIGHBOUR GRAPH — what the store looks like from the inside.
#
# A collection is a cloud of vectors, and the only structure in it that means
# anything is which points are near which. So the graph is built from the
# embeddings themselves: every item is joined to its k nearest neighbours by
# cosine similarity, and the edge carries that similarity.
#
# Two kinds of edge, kept distinct on purpose:
#   similarity — computed here, from the vectors
#   declared   — recorded by the store (a version superseding another)
# One is an observation and the other is a fact, and a view that blurred them
# would be showing something that is not true of the data.
# ---------------------------------------------------------------------------

MAX_NODES = 400  # passages, not documents — a single file is now many nodes
DEFAULT_K = 3

# How much weaker than the typical nearest-neighbour an edge may be before it
# is dropped. A FIXED cosine floor was wrong here: different embedding models
# occupy different parts of the range, and the one in use puts plainly related
# documents around 0.6-0.7, so a 0.45 constant borrowed from another model's
# distribution left two thirds of a collection with no edges at all. Taking the
# floor from the data keeps the graph meaningful whatever model a collection
# was built with.
RELATIVE_FLOOR = 0.72
# A last guard against a collection of unrelated documents being wired into a
# mesh: nothing below this is a neighbour under any model.
ABSOLUTE_FLOOR = 0.2


def _normalise(vector: list[float]) -> tuple[list[float], float]:
    length = math.sqrt(sum(v * v for v in vector))
    return vector, length


def _cosine(a: list[float], a_len: float, b: list[float], b_len: float) -> float:
    if a_len == 0 or b_len == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=False)) / (a_len * b_len)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def knn_edges(
    points: list[dict[str, Any]], k: int = DEFAULT_K
) -> tuple[list[dict[str, Any]], float]:
    """Join every point to its k nearest neighbours.

    Returns the edges and the floor that was applied, because a graph drawn
    with a threshold nobody can see is a graph nobody can argue with.

    Edges are deduplicated by unordered pair: if A lists B and B lists A, that
    is one relationship drawn once, keeping the stronger score.
    """
    prepared = [(p["id"], *_normalise(p["embedding"])) for p in points if p.get("embedding")]
    if len(prepared) < 2:
        return [], 0.0

    # Rank once, then decide the floor from what "near" actually looks like in
    # this collection rather than from a constant.
    ranked: dict[str, list[tuple[float, str]]] = {}
    for i, (id_a, vec_a, len_a) in enumerate(prepared):
        scored = [
            (_cosine(vec_a, len_a, vec_b, len_b), id_b)
            for j, (id_b, vec_b, len_b) in enumerate(prepared)
            if i != j
        ]
        scored.sort(reverse=True)
        ranked[id_a] = scored

    typical = _median([s[0][0] for s in ranked.values() if s])
    floor = max(ABSOLUTE_FLOOR, typical * RELATIVE_FLOOR)

    best: dict[tuple[str, str], float] = {}
    for id_a, scored in ranked.items():
        for similarity, id_b in scored[:k]:
            if similarity < floor:
                continue
            pair = (id_a, id_b) if id_a < id_b else (id_b, id_a)
            if similarity > best.get(pair, 0.0):
                best[pair] = similarity

    edges = [
        {"src": a, "dst": b, "similarity": round(s, 4), "kind": "similarity"}
        for (a, b), s in sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return edges, round(floor, 4)


async def collection_graph(
    session: AsyncSession, scope: Scope, k: int = DEFAULT_K, limit: int = MAX_NODES
) -> dict[str, Any]:
    """The collection as nodes and edges, ready to draw.

    A node is a PASSAGE, not a document. One node per file showed a collection
    of a dozen documents as a dozen unconnected dots — nothing to look at, and
    nothing true either, since a document is not one idea. Passages are what
    was embedded, so they are what the graph can honestly draw.

    Node degree is returned with the node because the view sizes points by how
    connected they are, and computing that in the browser would mean shipping
    the edge list twice.
    """
    points = await graph.collection_chunk_vectors(scope, limit=limit, live_only=True)
    if not points:
        return {
            "nodes": [], "edges": [], "truncated": False, "k": k, "floor": 0.0,
            "documents": 0,
            "projection": {"method": "none", "explained_variance": 0.0},
        }

    edges, floor = knn_edges(points, k=k)

    # Passages of the same document are joined explicitly. Without it a long
    # document reads as scattered unrelated points; with it the document is
    # visible as a shape, and a passage sitting far from its own siblings is
    # worth noticing — it usually means the file covers two subjects.
    by_item: dict[str, list[dict[str, Any]]] = {}
    for p in points:
        by_item.setdefault(p["item_id"], []).append(p)
    for siblings in by_item.values():
        ordered = sorted(siblings, key=lambda s: s["ordinal"])
        edges += [
            {"src": a["id"], "dst": b["id"], "similarity": 1.0, "kind": "same_document"}
            for a, b in zip(ordered, ordered[1:], strict=False)
        ]

    # The same vectors, flattened to two dimensions. Done here rather than in
    # its own endpoint so a collection's 1536-float embeddings are read once.
    flattened = project(points)

    # Documents give the cloud its colour, so the passages of one file share a
    # hue and the graph shows how they scatter.
    item_ids = list(by_item)
    tagged = await classes_for(session, scope, item_ids)

    degree: dict[str, int] = {}
    for e in edges:
        degree[e["src"]] = degree.get(e["src"], 0) + 1
        degree[e["dst"]] = degree.get(e["dst"], 0) + 1

    nodes = [
        {
            "id": p["id"],
            "itemId": p["item_id"],
            "ordinal": p["ordinal"],
            # fact | summary, and 1..4 for how far consolidation has carried
            # it. A summary is text a model wrote, so the view has to be able
            # to say so rather than drawing it like any other passage.
            "nodeType": p.get("node_type", "fact"),
            "stage": p.get("stage", 1),
            # What the passage is: its heading path, falling back to the
            # document title for a passage above the first heading.
            "title": p["heading"] or p["title"] or "",
            "document": p["title"] or "",
            "source": "",
            "degree": degree.get(p["id"], 0),
            "category": (tagged.get(p["item_id"]) or [{}])[0].get("class_id"),
            "categoryName": (tagged.get(p["item_id"]) or [{}])[0].get("name"),
            # Position in embedding space, 0..1. Distinct from the force
            # layout: these coordinates mean something.
            "px": flattened["coords"].get(p["id"], {}).get("x"),
            "py": flattened["coords"].get(p["id"], {}).get("y"),
        }
        for p in points
    ]

    return {
        "nodes": nodes,
        "edges": edges,
        "truncated": len(points) >= limit,
        "k": k,
        "documents": len(by_item),
        # Shown in the view: a threshold nobody can see is one nobody can argue with.
        "floor": floor,
        "projection": {
            "method": flattened["method"],
            "explained_variance": flattened["explained_variance"],
        },
    }


__all__ = [
    "ABSOLUTE_FLOOR",
    "DEFAULT_K",
    "MAX_NODES",
    "RELATIVE_FLOOR",
    "collection_graph",
    "knn_edges",
]
