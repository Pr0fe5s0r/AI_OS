from __future__ import annotations

import asyncio
import os

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import chunks, graph
from packages.shared.schema import Scope

# ---------------------------------------------------------------------------
# AUTHORED EDGES — the one thing similarity cannot reconstruct.
#
# The graph joins passages by cosine (:NEAR). A wikilink is a judgement:
# "this ELABORATES that", "this DEFINES the term that uses". Cosine cannot tell
# elaboration from contradiction — both are "similar". So a bounded pass asks
# the model to TYPE the near-neighbour pairs it already has, and writes a
# :RELATED edge carrying the relation. `neighbors()` then walks those alongside
# :NEAR and ranks them first — a reason two passages belong together beats a
# mere similarity score.
#
# Bounded like the probe-mapper: a fixed number of pairs per pass, spread across
# consolidation cycles, so the cost is paid down over time rather than all at
# once. Every model call is offloaded to a thread; a failure types the pair as
# 'none' and moves on, never stalling or crashing the pass. Opt-in.
# ---------------------------------------------------------------------------

TYPED_RELATIONS = ("elaborates", "defines", "supports", "contradicts", "precedes")
_VALID = set(TYPED_RELATIONS) | {"none"}
# A typed edge's stored weight. Deliberately above a typical NEAR similarity so
# that even a modestly-confident authored edge outranks cosine in the traversal.
_CONFIDENCE = 0.75

_PROMPT = (
    "Two passages from one collection are shown; they are already known to be "
    "similar. Decide how the FIRST relates to the SECOND as a judgement a reader "
    "would make — NOT mere shared topic. Reply with ONE word only:\n"
    "  elaborates  — the first expands on or adds detail to the second\n"
    "  defines     — the first defines a term or concept the second uses\n"
    "  supports    — the first gives evidence or an example for the second\n"
    "  contradicts — the first conflicts with or qualifies the second\n"
    "  precedes    — the first is a step or stage that comes before the second\n"
    "  none        — they merely share a topic, with no such relationship\n\n"
)


def typed_edges_enabled() -> bool:
    return os.getenv("TYPED_EDGES_ENABLED", "false").lower() in ("1", "true", "yes")


def max_pairs_per_pass() -> int:
    """How many pairs to type per consolidation pass. Bounds the model spend."""
    return max(1, int(os.getenv("TYPED_EDGES_PER_PASS", "20")))


def _classify(a_text: str, b_text: str) -> tuple[str, float]:
    """One model call → (relation, confidence). Blocking; call via a thread."""
    from packages.core.llm import chat

    content = f"{_PROMPT}FIRST:\n{a_text[:1200]}\n\nSECOND:\n{b_text[:1200]}"
    try:
        reply = chat([{"role": "user", "content": content}]).strip().lower()
    except Exception:
        return "none", 0.0
    word = reply.split()[0].strip(".,:;\"'()") if reply else "none"
    if word in _VALID and word != "none":
        return word, _CONFIDENCE
    return "none", 0.0


async def label_edges(
    session: AsyncSession, scope: Scope, limit: int | None = None
) -> int:
    """Type a bounded batch of unlabeled NEAR pairs. Returns how many got a real
    (non-'none') relation. Best-effort — nothing here may fail the caller."""
    limit = limit or max_pairs_per_pass()
    pairs = await graph.unlabeled_near_pairs(scope, limit=limit)
    if not pairs:
        return 0

    ids = {p["src"] for p in pairs} | {p["dst"] for p in pairs}
    stored = await chunks.by_ids(session, scope, list(ids))

    labelled = 0
    for pair in pairs:
        a, b = stored.get(pair["src"]), stored.get(pair["dst"])
        if a is None or b is None:
            # Text gone (re-indexed/deleted). Mark processed so it is not
            # re-picked forever, and move on.
            await graph.link_chunk(scope, pair["src"], pair["dst"], "none", 0.0)
            continue
        relation, confidence = await asyncio.to_thread(_classify, a.text, b.text)
        await graph.link_chunk(scope, pair["src"], pair["dst"], relation, confidence)
        if relation != "none":
            labelled += 1
    return labelled


__all__ = [
    "TYPED_RELATIONS",
    "label_edges",
    "max_pairs_per_pass",
    "typed_edges_enabled",
]
