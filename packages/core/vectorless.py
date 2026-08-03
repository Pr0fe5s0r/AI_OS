from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import tree
from packages.core.search import Trace, new_trace_id
from packages.shared.schema import Hit, Lifecycle, Passage, Scope, SourceRef

# ---------------------------------------------------------------------------
# VECTORLESS RETRIEVAL — choose what to read, then read it.
#
# The method is PageIndex's: build a table-of-contents tree from each document
# and let a model reason over that structure to pick the sections worth
# opening, instead of ranking chunks by cosine distance. Similarity is not
# relevance. For a long structured document the author already labelled what is
# where, and a heading someone wrote on purpose carries more information about
# content than a distance between two vectors.
#
# Two model calls and no more, deliberately:
#
#   1. SELECT — given the outlines (titles and sizes, no text), which sections
#      answer this question?
#   2. ANSWER — handled by the existing answer path, unchanged.
#
# An agentic loop that browses the tree over many turns is what the reference
# implementation demonstrates, and it is better on very large corpora. It is
# also several times the latency and cost for a collection this size, so the
# selection is done in one pass and the loop is left as a later choice rather
# than a default nobody asked for.
#
# The trees are built per request, not stored. Building one is regex over a
# string — microseconds — so a table to keep them in would be a table that can
# go stale, for no gain.
#
# What comes back is the same Hit/Passage shape hybrid search returns, so the
# answering, citation and console layers do not know or care which retrieval
# produced it.
# ---------------------------------------------------------------------------

# How many documents' outlines go in front of the model at once. Past this the
# outline itself stops fitting, and picking sections from a list nobody can
# read is guesswork wearing a suit.
MAX_DOCUMENTS = 40
# How many sections may be opened for one question.
MAX_SECTIONS = 6

# How many sections the model is told to return. Asked to use its judgement
# about how many, this model returned one — and the one it returned was wrong,
# matching "Required source types" to a question about required connectors on
# the shared word while ignoring "Integration with Drive Sync", which shares no
# words and holds the answer.
#
# So it is told a number instead. At selection time recall beats precision: the
# answering step reads all of them and cites only what it uses, and refuses
# outright if none of them support an answer. A wrong section costs a few
# hundred tokens; a missing one costs the answer.
TARGET_SECTIONS = 3

_SELECT_SYSTEM = (
    "You choose which sections of a document to read in order to answer a "
    "question. You reason about what a section CONTAINS from its title and its "
    "position in the structure. You never match on shared words: a section "
    "called 'Required source types' and a question about 'required connectors' "
    "share a word and may be unrelated, while 'Integration with Drive Sync' "
    "shares none and may be exactly right. You reply with JSON and nothing else."
)


def _select_prompt(outline: str, question: str, available: int) -> str:
    """The instruction, with the count stated as a number.

    The count lives in the user message beside the question rather than in the
    system prompt, because that is where this model actually honours it.
    """
    want = min(TARGET_SECTIONS, max(1, available))
    return (
        f"DOCUMENTS AND THEIR SECTIONS:\n{outline}\n\n"
        f"QUESTION: {question}\n\n"
        f"Return the {want} sections most likely to contain the answer, best "
        "first. Prefer a specific subsection over a broad parent, unless the "
        "answer is spread across that parent's subsections. If nothing in these "
        "documents could possibly answer the question, return an empty list.\n\n"
        'Reply with JSON only: {"sections": [{"doc": "<doc id>", "id": '
        '"<section id>", "why": "<six words or fewer>"}]}'
    )


@dataclass(slots=True)
class Selection:
    """What the model chose to read, and why it said it chose it."""

    item_id: str
    node_id: str
    why: str = ""


@dataclass(slots=True)
class Outcome:
    hits: list[Hit] = field(default_factory=list)
    selections: list[Selection] = field(default_factory=list)
    documents_considered: int = 0
    sections_available: int = 0
    outline_tokens: int = 0
    degraded: str | None = None


async def _documents(session: AsyncSession, scope: Scope) -> list[dict[str, Any]]:
    clause = "AND collection_id = :c" if scope.collection_id else ""
    rows = (
        await session.execute(
            sql(
                f"""
                SELECT item_id, title, body, source, locator, url
                FROM kb_items
                WHERE workspace_id = :w AND status = :active {clause}
                ORDER BY created_at DESC
                LIMIT :limit
                """  # noqa: S608 - clause is a fixed literal, not input
            ),
            {
                "w": scope.workspace_id,
                "c": scope.collection_id,
                "active": str(Lifecycle.ACTIVE),
                "limit": MAX_DOCUMENTS,
            },
        )
    ).all()
    return [
        {
            "item_id": r.item_id,
            "title": r.title,
            "body": r.body,
            "source": r.source,
            "locator": r.locator,
            "url": r.url,
        }
        for r in rows
    ]


def _outline(documents: list[dict[str, Any]], trees: dict[str, tree.Node]) -> str:
    """Every document's structure, titles only. No text — that is the point."""
    return json.dumps(
        [
            {
                "doc": doc["item_id"],
                "title": doc["title"],
                "sections": trees[doc["item_id"]].outline()["sections"]
                if "sections" in trees[doc["item_id"]].outline()
                else [],
            }
            for doc in documents
        ],
        ensure_ascii=False,
    )


def _parse(raw: str) -> list[Selection]:
    """Read the model's choice, tolerating the wrappers models add.

    A malformed reply means nothing was selected, which surfaces as "nothing
    here answers that" — the honest outcome. Guessing at sections from a broken
    reply would be inventing a retrieval nobody performed.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return []
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return []

    # The reply shape is genuinely unstable across calls at temperature zero:
    # {"sections":[{...}]}, a bare ["n014"], or a single {"section":"n016",…}
    # with no list at all. Each of those was produced by the same model on the
    # same prompt. Only the ids in the reply are a contract — the container
    # around them is not — and a parser that accepted one shape reported
    # "nothing answers that" while the model was naming the right section.
    chosen: Any = payload
    if isinstance(payload, dict):
        for key in ("sections", "results", "selected", "nodes"):
            if isinstance(payload.get(key), list):
                chosen = payload[key]
                break
        else:
            # No list anywhere: the dict is itself one selection.
            chosen = [payload] if payload else []
    if not isinstance(chosen, list):
        return []

    # The shape varies between calls even at temperature zero: sometimes
    # {"doc":…, "id":…} objects, sometimes bare "n014" strings. A parser that
    # accepted only the documented shape silently discarded every selection and
    # reported "nothing answers that" — a wrong answer produced by strictness,
    # which is the worst kind because it looks like a considered result. The
    # model's output shape is not a contract; the ids in it are.
    out: list[Selection] = []
    for entry in chosen[:MAX_SECTIONS]:
        if isinstance(entry, str):
            out.append(Selection(item_id="", node_id=entry, why=""))
            continue
        if not isinstance(entry, dict):
            continue
        node = (
            entry.get("id")
            or entry.get("section")
            or entry.get("node_id")
            or entry.get("section_id")
        )
        if not isinstance(node, str):
            continue
        doc = entry.get("doc") or entry.get("document") or entry.get("doc_id")
        out.append(
            Selection(
                item_id=doc if isinstance(doc, str) else "",
                node_id=node,
                why=str(entry.get("why") or entry.get("reason") or ""),
            )
        )
    return out


def _resolve(
    selections: list[Selection], trees: dict[str, tree.Node]
) -> list[Selection]:
    """Attach a document to any selection that arrived without one.

    Section ids restart at n000 in every document, so a bare id is only
    meaningful when exactly one document owns it. When several do, the
    selection is ambiguous and is dropped rather than guessed — attributing a
    passage to the wrong document would put a citation under a title it never
    came from.
    """
    resolved: list[Selection] = []
    for choice in selections:
        if choice.item_id and choice.item_id in trees:
            resolved.append(choice)
            continue
        owners = [
            item_id
            for item_id, root in trees.items()
            if any(n.node_id == choice.node_id for n in root.walk())
        ]
        if len(owners) == 1:
            resolved.append(
                Selection(item_id=owners[0], node_id=choice.node_id, why=choice.why)
            )
    return resolved


async def retrieve(session: AsyncSession, scope: Scope, question: str) -> tuple[Outcome, Trace]:
    """Pick sections by reasoning over structure, then return them as hits.

    Returns the same shape hybrid search does, so everything downstream —
    answering, citations, the console — is unchanged by the swap.
    """
    started = time.perf_counter()
    outcome = Outcome()
    trace = Trace(
        trace_id=new_trace_id(),
        query=question,
        config={"retrieval": "vectorless", "max_sections": MAX_SECTIONS},
        filters={"workspace_id": scope.workspace_id, "collection_id": scope.collection_id},
    )

    mark = time.perf_counter()
    documents = await _documents(session, scope)
    trace.timings_ms["load"] = int((time.perf_counter() - mark) * 1000)
    if not documents:
        trace.timings_ms["total"] = int((time.perf_counter() - started) * 1000)
        return outcome, trace

    mark = time.perf_counter()
    trees = {doc["item_id"]: tree.build(doc["body"], doc["title"]) for doc in documents}
    outline = _outline(documents, trees)
    trace.timings_ms["index"] = int((time.perf_counter() - mark) * 1000)

    outcome.documents_considered = len(documents)
    outcome.sections_available = sum(tree.count(t) for t in trees.values())
    outcome.outline_tokens = len(outline) // tree.CHARS_PER_TOKEN

    from packages.core.llm import chat

    mark = time.perf_counter()
    messages = [
        {"role": "system", "content": _SELECT_SYSTEM},
        {"role": "user", "content": f"DOCUMENTS:\n{outline}\n\nQUESTION: {question}"},
    ]
    try:
        # JSON mode matters more here than it looks. Asked in prose for "2 to 4
        # sections" this model returned one — and the one it returned was
        # wrong, matching 'Required source types' to a question about required
        # connectors on the shared word. Constrained to JSON it returns the
        # several it was asked for, including the section that actually
        # answers. Not every provider supports the flag, so a rejection falls
        # back to the same call without it rather than failing the request.
        try:
            raw = await asyncio.to_thread(
                chat, messages, temperature=0.0, response_format={"type": "json_object"}
            )
        except Exception:
            raw = await asyncio.to_thread(chat, messages, temperature=0.0)
    except Exception as exc:
        # No fallback to vector search here. Silently answering by a different
        # method than the one asked for is exactly the kind of substitution
        # that makes a result impossible to interpret.
        outcome.degraded = f"section selection unavailable: {type(exc).__name__}"
        trace.degraded = outcome.degraded
        trace.timings_ms["total"] = int((time.perf_counter() - started) * 1000)
        return outcome, trace
    trace.timings_ms["select"] = int((time.perf_counter() - mark) * 1000)

    outcome.selections = _resolve(_parse(raw), trees)
    trace.semantic = [
        {"item_id": s.item_id, "node_id": s.node_id, "why": s.why} for s in outcome.selections
    ]

    by_id = {doc["item_id"]: doc for doc in documents}
    grouped: dict[str, list[Passage]] = {}
    for rank, choice in enumerate(outcome.selections):
        root = trees.get(choice.item_id)
        doc = by_id.get(choice.item_id)
        if root is None or doc is None:
            continue
        nodes = tree.find(root, [choice.node_id])
        if not nodes:
            # An id the model invented. Dropped rather than substituted: a
            # section that does not exist cannot be evidence for anything.
            continue
        node = nodes[0]
        body = tree.section_text(node)
        if not body:
            continue
        grouped.setdefault(choice.item_id, []).append(
            Passage(
                chunk_id=f"{choice.item_id}:{node.node_id}",
                ordinal=rank,
                heading=node.title,
                text=body,
                # Order of choice IS the ranking — the model was asked for
                # fewest and best first. There is no distance to report, and
                # inventing a score would imply a measurement nobody made.
                score=round(1.0 - rank * 0.05, 4),
            )
        )

    outcome.hits = [
        Hit(
            item_id=item_id,
            title=by_id[item_id]["title"],
            excerpt=passages[0].text[:600],
            source=SourceRef(
                source=by_id[item_id]["source"],
                locator=by_id[item_id]["locator"],
                url=by_id[item_id]["url"],
            ),
            score=passages[0].score,
            semantic=0.0,
            keyword=0.0,
            heading=passages[0].heading,
            passages=passages,
        )
        for item_id, passages in grouped.items()
    ]
    outcome.hits.sort(key=lambda h: h.score, reverse=True)

    trace.returned = [h.item_id for h in outcome.hits]
    trace.fused = [
        {
            "item_id": p.chunk_id.split(":")[0],
            "chunk_id": p.chunk_id,
            "heading": p.heading,
            "score": p.score,
            "kept": True,
        }
        for h in outcome.hits
        for p in h.passages
    ]
    trace.timings_ms["total"] = int((time.perf_counter() - started) * 1000)
    return outcome, trace


__all__ = [
    "MAX_DOCUMENTS",
    "MAX_SECTIONS",
    "TARGET_SECTIONS",
    "Outcome",
    "Selection",
    "retrieve",
]
