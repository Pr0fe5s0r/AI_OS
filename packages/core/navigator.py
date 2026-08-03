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
# THE NAVIGATOR — an agent that reads its way to an answer.
#
# This replaces a single "which sections should I read?" call that asked the
# model to commit to an answer before seeing any content. That call was
# unreliable in a way that looked exactly like an empty store: asked what a
# document titled "Value Education" covered, it selected nothing at all and the
# console said no section looked relevant. Same model, same prompt, different
# run — sometimes a good section, sometimes a wrong one, sometimes none.
#
# The failure was structural, not a bad prompt. One shot with no feedback gives
# the model no way to notice it guessed wrong, and free-form JSON gives the
# provider no schema to enforce, so "no sections" and "malformed reply" arrive
# looking identical.
#
# So it navigates instead, the way the reference implementation does:
#
#     open_document(doc)   the table of contents for one document
#     read_section(doc, id)  the text of a section, numbered for citing
#     answer(text)           finish, citing the numbers it was given
#
# Tool calls are schema-checked by the provider, so a malformed one is rejected
# at the source rather than parsed into silence. And because reading is a
# separate turn from deciding, a wrong first guess is recoverable: the model
# sees the section was not what it wanted and reads another.
#
# Citations are assigned HERE, not by the model. Every section it reads is
# handed back with the next number in sequence, so [2] cannot refer to anything
# but the second thing actually read. A model that invents [7] is inventing a
# reference to a section nobody opened, and that marker is dropped downstream.
# ---------------------------------------------------------------------------

# How many documents' outlines go in front of the model at once. Past this the
# catalogue itself stops fitting, and choosing from a list nobody can read is
# guesswork wearing a suit.
MAX_DOCUMENTS = 40
# Each round is one model call. Enough to open a document, read two or three
# sections and answer; short enough that a model looping on itself stops.
MAX_ROUNDS = 6
# Reading the same section twice is a loop, not research.
MAX_READS = 6
MAX_SECTION_CHARS = 4000

_SYSTEM = (
    "You answer questions from a document store by navigating it, like a person "
    "who knows where to look.\n\n"
    "How to work:\n"
    "1. You are given every document's title and its table of contents.\n"
    "2. Call read_section on the sections most likely to hold the answer. Judge "
    "by what a section CONTAINS, not by words it shares with the question.\n"
    "3. Read more than one when unsure. Reading a section that turns out to be "
    "irrelevant costs nothing; missing one loses the answer.\n"
    "4. If what you read is not enough, read another section before answering.\n"
    "5. Call submit_answer when you can answer, or when nothing in the store "
    "can.\n\n"
    "Every section you read is given a number. Cite those numbers in your "
    "answer like [1] or [2][3]. Cite only numbers you were actually given. "
    "Never state anything the sections do not say. If the store does not "
    "answer the question, say so plainly in submit_answer and cite nothing."
)

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_section",
            "description": (
                "Read the full text of one section. Returns the text with a "
                "citation number to use in your answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doc": {"type": "string", "description": "The document id."},
                    "section": {"type": "string", "description": "The section id, e.g. n014."},
                },
                "required": ["doc", "section"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_answer",
            "description": (
                "Give the final answer, citing the numbered sections you read. "
                "Use this also to say the store does not answer the question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {"type": "string"},
                    "found": {
                        "type": "boolean",
                        "description": "False if the sections do not answer the question.",
                    },
                },
                "required": ["answer", "found"],
            },
        },
    },
]


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


@dataclass(slots=True)
class Step:
    """One thing the agent did, for the trace."""

    round: int
    action: str
    detail: str


@dataclass(slots=True)
class Outcome:
    answer: str = ""
    found: bool = False
    hits: list[Hit] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    documents_considered: int = 0
    sections_available: int = 0
    rounds: int = 0
    degraded: str | None = None


def _catalogue(documents: list[dict[str, Any]], trees: dict[str, tree.Node]) -> str:
    """Every document and its sections. Titles and openings, never full text."""
    return json.dumps(
        [
            {
                "doc": doc["item_id"],
                "title": doc["title"],
                "sections": trees[doc["item_id"]].outline().get("sections", []),
            }
            for doc in documents
        ],
        ensure_ascii=False,
    )


async def navigate(
    session: AsyncSession, scope: Scope, question: str
) -> tuple[Outcome, Trace]:
    """Let the model read its way to an answer, and record every step."""
    started = time.perf_counter()
    outcome = Outcome()
    trace = Trace(
        trace_id=new_trace_id(),
        query=question,
        config={"retrieval": "vectorless", "max_rounds": MAX_ROUNDS},
        filters={"workspace_id": scope.workspace_id, "collection_id": scope.collection_id},
    )

    documents = await _documents(session, scope)
    if not documents:
        trace.timings_ms["total"] = int((time.perf_counter() - started) * 1000)
        return outcome, trace

    trees = {doc["item_id"]: tree.build(doc["body"], doc["title"]) for doc in documents}
    by_id = {doc["item_id"]: doc for doc in documents}
    outcome.documents_considered = len(documents)
    outcome.sections_available = sum(tree.count(t) for t in trees.values())

    from packages.core.llm import chat_with_tools

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": (
                f"DOCUMENTS:\n{_catalogue(documents, trees)}\n\nQUESTION: {question}"
            ),
        },
    ]

    # Numbered in the order they are read, so a citation can only ever point at
    # something that was actually opened.
    read: list[tuple[Passage, str]] = []
    seen: set[tuple[str, str]] = set()

    mark = time.perf_counter()
    for round_number in range(1, MAX_ROUNDS + 1):
        outcome.rounds = round_number
        try:
            reply = await asyncio.to_thread(chat_with_tools, messages, _TOOLS, temperature=0.0)
        except Exception as exc:
            outcome.degraded = f"navigation unavailable: {type(exc).__name__}"
            trace.degraded = outcome.degraded
            break

        calls = reply.get("tool_calls") or []
        if not calls:
            # It answered in prose without calling submit_answer. Taken as the
            # answer rather than discarded — refusing content the model already
            # produced would turn a formatting slip into an empty result, which
            # is the failure this whole module exists to remove.
            if reply.get("content"):
                outcome.answer = reply["content"].strip()
                outcome.found = bool(read)
                outcome.steps.append(Step(round_number, "answered", "without submit_answer"))
            break

        messages.append(
            {
                "role": "assistant",
                "content": reply.get("content") or "",
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["arguments"]},
                    }
                    for c in calls
                ],
            }
        )

        finished = False
        for call in calls:
            try:
                args = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}

            if call["name"] == "submit_answer":
                outcome.answer = str(args.get("answer") or "").strip()
                outcome.found = bool(args.get("found")) and bool(read)
                outcome.steps.append(
                    Step(round_number, "answered", "found" if outcome.found else "not found")
                )
                finished = True
                break

            if call["name"] != "read_section":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": f"No such tool: {call['name']}",
                    }
                )
                continue

            doc_id = str(args.get("doc") or "")
            node_id = str(args.get("section") or "")
            root = trees.get(doc_id)
            if root is None and len(trees) == 1:
                # A section id with the wrong document attached, when there is
                # only one document it could belong to. Correcting that is not
                # a guess.
                doc_id, root = next(iter(trees.items()))

            nodes = tree.find(root, [node_id]) if root else []
            if not nodes:
                # Said plainly so the model can try another rather than
                # treating silence as "nothing is there".
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": (
                            f"No section {node_id!r} in that document. "
                            "Check the ids in the catalogue and try another."
                        ),
                    }
                )
                outcome.steps.append(Step(round_number, "missed", node_id))
                continue

            node = nodes[0]
            if (doc_id, node_id) in seen:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": "Already read. Read a different section, or answer.",
                    }
                )
                continue
            seen.add((doc_id, node_id))

            body = tree.section_text(node, limit=MAX_SECTION_CHARS)
            marker = len(read) + 1
            read.append(
                (
                    Passage(
                        chunk_id=f"{doc_id}:{node.node_id}",
                        ordinal=marker - 1,
                        heading=node.title,
                        text=body,
                        score=round(1.0 - (marker - 1) * 0.05, 4),
                    ),
                    doc_id,
                )
            )
            outcome.steps.append(Step(round_number, "read", f"{node.title[:60]} → [{marker}]"))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": (
                        f"[{marker}] {by_id[doc_id]['title']} > {node.title}\n\n{body}\n\n"
                        f"(Cite this as [{marker}].)"
                    ),
                }
            )

            if len(read) >= MAX_READS:
                messages.append(
                    {
                        "role": "user",
                        "content": "That is enough reading. Answer now with submit_answer.",
                    }
                )

        if finished:
            break

    trace.timings_ms["navigate"] = int((time.perf_counter() - mark) * 1000)

    # Group what was read into hits, so everything downstream — citations, the
    # console, the trace — sees the same shape every other retrieval produces.
    grouped: dict[str, list[Passage]] = {}
    for passage, doc_id in read:
        grouped.setdefault(doc_id, []).append(passage)

    outcome.hits = [
        Hit(
            item_id=doc_id,
            title=by_id[doc_id]["title"],
            excerpt=passages[0].text[:600],
            source=SourceRef(
                source=by_id[doc_id]["source"],
                locator=by_id[doc_id]["locator"],
                url=by_id[doc_id]["url"],
            ),
            score=passages[0].score,
            heading=passages[0].heading,
            passages=passages,
        )
        for doc_id, passages in grouped.items()
    ]

    trace.semantic = [
        {"step": s.round, "action": s.action, "detail": s.detail} for s in outcome.steps
    ]
    trace.returned = [h.item_id for h in outcome.hits]
    trace.fused = [
        {
            "item_id": doc_id,
            "chunk_id": p.chunk_id,
            "heading": p.heading,
            "score": p.score,
            "kept": True,
        }
        for doc_id, passages in grouped.items()
        for p in passages
    ]
    trace.timings_ms["total"] = int((time.perf_counter() - started) * 1000)
    return outcome, trace


__all__ = ["MAX_DOCUMENTS", "MAX_READS", "MAX_ROUNDS", "Outcome", "Step", "navigate"]
