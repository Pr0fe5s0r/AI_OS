from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# PROJECTION — the collection flattened to two dimensions.
#
# The neighbour graph shows which documents are near each other; it says
# nothing about how far apart the rest are, because a force layout's distances
# are an artefact of the simulation. This does the other job: place every
# document by its actual position in embedding space, so proximity on screen
# means proximity in the vectors.
#
# PCA, not UMAP or t-SNE. Two reasons. It is deterministic — the same
# collection always lands the same way, which matters for a console someone
# returns to — and it preserves global structure, so the distance between two
# clusters is meaningful rather than arbitrary. t-SNE would give prettier,
# tighter blobs and lie about how far apart they are.
#
# It also reports how much variance the two axes actually captured, because a
# 2D view of 1536 dimensions always loses something and the honest thing is to
# say how much.
# ---------------------------------------------------------------------------


def project(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Flatten embeddings to 2D coordinates in a 0..1 box.

    Returns the coordinates keyed by item id, plus the share of variance the
    two axes explain. Fewer than three points cannot be projected meaningfully,
    so they are laid out plainly instead of being given false positions.
    """
    usable = [p for p in points if p.get("embedding")]
    if len(usable) < 3:
        return {
            "coords": {
                p["id"]: {"x": 0.5, "y": 0.5 if len(usable) == 1 else 0.25 + 0.5 * i}
                for i, p in enumerate(usable)
            },
            "explained_variance": 0.0,
            "method": "none",
        }

    import numpy as np

    matrix = np.asarray([p["embedding"] for p in usable], dtype=np.float64)

    # Cosine similarity is what the store ranks by, so the projection has to
    # agree with it: normalising to unit length first makes Euclidean distance
    # in this space a monotone function of cosine distance. Without it, longer
    # vectors would drift to the edges for no reason a reader could interpret.
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = matrix / np.where(norms == 0, 1, norms)

    centred = matrix - matrix.mean(axis=0)
    # SVD rather than an eigendecomposition of the covariance matrix: it is the
    # numerically stable route, and at these sizes the cost is irrelevant.
    _, singular, components = np.linalg.svd(centred, full_matrices=False)

    coords = centred @ components[:2].T
    total = float((singular**2).sum())
    explained = float((singular[:2] ** 2).sum() / total) if total > 0 else 0.0

    # Normalised to a unit box so the client can scale to whatever space it
    # has without knowing anything about the embedding model.
    lo = coords.min(axis=0)
    span = np.where(coords.max(axis=0) - lo == 0, 1, coords.max(axis=0) - lo)
    unit = (coords - lo) / span

    return {
        "coords": {
            p["id"]: {"x": round(float(unit[i][0]), 4), "y": round(float(unit[i][1]), 4)}
            for i, p in enumerate(usable)
        },
        "explained_variance": round(explained, 4),
        "method": "pca",
    }


__all__ = ["project"]
