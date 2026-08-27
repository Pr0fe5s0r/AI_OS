"""The index-graph maths, checked against the obvious slow version.

Both functions here were rewritten for speed: the neighbour search from a
Python double loop into one matrix product (4.34s -> 0.047s on 200 passages),
and the projection from a full SVD into an eigendecomposition of the Gram
matrix (3.89s -> 0.255s). Together they took the index-graph endpoint from
10.2s to 0.85s.

Neither rewrite is allowed to change what is drawn, and neither is readable
enough to check by eye any more. So each is tested against a naive
implementation written the obvious way — slow, wrong to ship, and easy to
agree is correct.
"""

from __future__ import annotations

import math
import random

import numpy as np

from packages.core.neighbours import ABSOLUTE_FLOOR, RELATIVE_FLOOR, knn_edges
from packages.core.projection import project


def _points(n: int, dim: int = 24, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    return [
        {
            "id": f"c{i:03d}",
            "item_id": f"doc{i % 4}",
            "ordinal": i,
            "embedding": [rng.gauss(0, 1) for _ in range(dim)],
        }
        for i in range(n)
    ]


def _naive_knn(points: list[dict], k: int = 3) -> tuple[set[tuple[str, str]], float]:
    """Every pair, compared one at a time. The version the fast one must match."""

    def cosine(a: list[float], b: list[float]) -> float:
        la = math.sqrt(sum(x * x for x in a))
        lb = math.sqrt(sum(x * x for x in b))
        if la == 0 or lb == 0:
            return 0.0
        return sum(x * y for x, y in zip(a, b, strict=True)) / (la * lb)

    usable = [p for p in points if p.get("embedding")]
    ranked: dict[str, list[tuple[float, str]]] = {}
    for i, a in enumerate(usable):
        scored = [
            (cosine(a["embedding"], b["embedding"]), b["id"])
            for j, b in enumerate(usable)
            if i != j
        ]
        scored.sort(reverse=True)
        ranked[a["id"]] = scored

    tops = sorted(s[0][0] for s in ranked.values() if s)
    middle = len(tops) // 2
    typical = 0.0 if not tops else (
        tops[middle] if len(tops) % 2 else (tops[middle - 1] + tops[middle]) / 2
    )
    floor = max(ABSOLUTE_FLOOR, typical * RELATIVE_FLOOR)

    pairs: set[tuple[str, str]] = set()
    for id_a, scored in ranked.items():
        for similarity, id_b in scored[:k]:
            if similarity >= floor:
                pairs.add((id_a, id_b) if id_a < id_b else (id_b, id_a))
    return pairs, round(floor, 4)


def test_the_fast_neighbour_search_draws_the_same_graph():
    """One matrix product instead of 19,900 dot products in Python. Same
    arithmetic — the interpreter was the cost, not the mathematics."""
    for n in (2, 3, 9, 40):
        points = _points(n)
        expected, expected_floor = _naive_knn(points)
        edges, floor = knn_edges(points)

        assert {(e["src"], e["dst"]) for e in edges} == expected, f"edge set differs at n={n}"
        assert floor == expected_floor, f"floor differs at n={n}"


def test_similarities_are_reported_to_the_same_precision():
    """float32 halved the memory and moved the reported similarity in the
    fourth decimal, which is where a near-tie flips an edge. float64 keeps the
    numbers the naive version produced."""
    points = _points(30)
    edges, _ = knn_edges(points)
    lookup = {(e["src"], e["dst"]): e["similarity"] for e in edges}

    by_id = {p["id"]: p["embedding"] for p in points}
    for (a, b), reported in lookup.items():
        va, vb = by_id[a], by_id[b]
        exact = sum(x * y for x, y in zip(va, vb, strict=True)) / (
            math.sqrt(sum(x * x for x in va)) * math.sqrt(sum(x * x for x in vb))
        )
        assert round(exact, 4) == reported


def test_a_point_is_never_its_own_neighbour():
    """Self-similarity is 1.0 and would win every ranking. The diagonal is
    excluded before the top-k, not filtered out afterwards."""
    points = _points(12)
    edges, _ = knn_edges(points)
    assert all(e["src"] != e["dst"] for e in edges)


def test_a_vector_of_zeroes_is_near_nothing():
    """It has no direction. Dividing by its length would produce NaN and NaN
    compares false against every threshold, so it would vanish silently rather
    than being reported as unrelated."""
    points = _points(6)
    points.append({"id": "zero", "item_id": "d", "ordinal": 99, "embedding": [0.0] * 24})

    edges, _ = knn_edges(points)
    assert all("zero" not in (e["src"], e["dst"]) for e in edges)
    assert all(not math.isnan(e["similarity"]) for e in edges)


def test_too_few_points_to_compare():
    assert knn_edges([]) == ([], 0.0)
    assert knn_edges(_points(1)) == ([], 0.0)
    # A point carrying no vector cannot be placed and must not count towards
    # the two needed to draw anything.
    only_one = [*_points(1), {"id": "x", "item_id": "d", "ordinal": 1, "embedding": []}]
    assert knn_edges(only_one) == ([], 0.0)


def test_the_projection_keeps_the_shape_the_svd_produced():
    """Gram-matrix eigendecomposition rather than SVD of the full matrix — the
    same decomposition seen from the other side, and 15x faster here.

    Compared by pairwise distance because an eigenvector's sign is arbitrary:
    the layout may come back mirrored, and a mirror is the same shape.
    """
    points = _points(25, dim=16)
    usable = np.asarray([p["embedding"] for p in points], dtype=np.float64)
    usable = usable / np.linalg.norm(usable, axis=1, keepdims=True)
    centred = usable - usable.mean(axis=0)
    _, singular, components = np.linalg.svd(centred, full_matrices=False)
    reference = centred @ components[:2].T
    lo = reference.min(axis=0)
    span = np.where(reference.max(axis=0) - lo == 0, 1, reference.max(axis=0) - lo)
    reference = (reference - lo) / span

    result = project(points)
    got = np.array([[result["coords"][p["id"]]["x"], result["coords"][p["id"]]["y"]] for p in points])

    ref_d = np.linalg.norm(reference[:, None] - reference[None], axis=-1)
    got_d = np.linalg.norm(got[:, None] - got[None], axis=-1)
    assert np.abs(ref_d - got_d).max() < 1e-3, "the projection is a different shape"

    total = float((singular**2).sum())
    assert abs(result["explained_variance"] - round(float((singular[:2] ** 2).sum() / total), 4)) < 1e-3


def test_the_same_collection_always_lands_the_same_way():
    """Determinism is the property PCA was chosen for over t-SNE. An
    eigenvector's sign is arbitrary, so without pinning it the graph could come
    back mirrored between two loads of the same page."""
    points = _points(20, dim=16)
    first = project(points)
    second = project(points)
    assert first["coords"] == second["coords"]
    assert first["explained_variance"] == second["explained_variance"]


def test_coordinates_stay_inside_the_unit_box():
    """The client scales these to whatever space it has and knows nothing about
    the embedding model."""
    result = project(_points(30, dim=16))
    for coord in result["coords"].values():
        assert 0.0 <= coord["x"] <= 1.0
        assert 0.0 <= coord["y"] <= 1.0
    assert result["method"] == "pca"


def test_too_few_points_are_laid_out_plainly_not_projected():
    """Two points cannot have a meaningful principal axis. Giving them one
    would be inventing a position and drawing it as though it meant something."""
    for n in (0, 1, 2):
        result = project(_points(n, dim=16))
        assert result["method"] == "none"
        assert result["explained_variance"] == 0.0
