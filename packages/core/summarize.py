from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import chunks, derive, graph
from packages.shared.schema import Scope

# ---------------------------------------------------------------------------
# INDEX SUMMARIES — a navigable semantic layer over a document's passages.
#
# For each document: one summary per section (what it CONTAINS, so the agent can
# decide whether to open it) and one card for the whole file. These are the
# richer catalogue the navigator reads to enter a collection — better than raw
# headings, and present even when a document has none.
#
# They are model-written, so they follow the same rule every summary in this
# store follows: they are for NAVIGATION, they carry the chunk ids they cover
# (`merged_from`), and retrieval filters them out so an answer's evidence is
# always a real passage. The covered passages are NOT archived — a card is laid
# OVER the facts, not in place of them.
#
# Text lives in Postgres; the node, its vector and the SUMMARIZES edges live in
# the graph (packages/core/graph.py).
# ---------------------------------------------------------------------------

_SECTION_PROMPT = (
    "Summarise what this section of a document CONTAINS, in one or two sentences, "
    "so a reader deciding whether to open it knows what they would find. Name the "
    "specific topics, entities, figures or steps it covers. Add nothing that is "
    "not in the text, and do not write phrases like 'this section'. Reply with "
    "the summary only.\n\nSECTION:\n"
)
_CARD_PROMPT = (
    "Write a two to three sentence card for a document, from its section "
    "summaries below: what the document is about and what it covers, concretely, "
    "so a reader can tell at a glance whether it holds what they need. Invent "
    "nothing. Reply with the card only.\n\nSECTION SUMMARIES:\n"
)

# A document with an unusual number of headings would otherwise fire a model
# call per section on upload. Past this, the tail is left for the probe-mapper
# to pick up over time rather than paid for all at once.
MAX_SECTIONS = 40


def summary_chunk_id(kind: str, covers: list[str]) -> str:
    """Deterministic from what it covers, so regenerating lands on the same node
    rather than piling up near-identical summaries."""
    prefix = "card" if kind == "card" else "sec"
    seed = kind + "\x1f" + "\x1f".join(sorted(covers))
    return f"{prefix}-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:28]


async def write_summary(
    session: AsyncSession,
    scope: Scope,
    *,
    chunk_id: str,
    item_id: str | None,
    kind: str,
    heading: str,
    body: str,
    covers: list[str],
    generated_by: str,
    probe_question: str | None = None,
) -> None:
    """Persist a summary: text in Postgres, node + vector + edges in the graph."""
    from packages.core.llm import embed

    await session.execute(
        text(
            """
            INSERT INTO kb_chunks
                (chunk_id, workspace_id, collection_id, item_id, version, ordinal,
                 heading, text, node_type, importance, stage, merged_from,
                 generated_by, probe_question)
            VALUES (:cid, :w, :c, :item, NULL, 0, :heading, :text, :kind, 0.5, 3,
                    CAST(:covers AS jsonb), :gen, :probe)
            ON CONFLICT (chunk_id) DO UPDATE
                SET text = EXCLUDED.text,
                    heading = EXCLUDED.heading,
                    node_type = EXCLUDED.node_type,
                    merged_from = EXCLUDED.merged_from,
                    generated_by = EXCLUDED.generated_by,
                    probe_question = EXCLUDED.probe_question,
                    archived_at = NULL
            """
        ),
        {
            "cid": chunk_id, "w": scope.workspace_id, "c": scope.collection_id,
            "item": item_id, "heading": heading[:500], "text": body, "kind": kind,
            "covers": json.dumps(covers), "gen": generated_by, "probe": probe_question,
        },
    )
    # Off the event loop: embedding is a blocking network call, and running it
    # inline would stall the worker (or, on the on-demand path, the API) for
    # every summary. A thread keeps the loop free and failures contained.
    vector = await asyncio.to_thread(embed, f"{heading}\n\n{body}" if heading else body)
    await graph.upsert_index_summary(
        scope,
        chunk_id=chunk_id,
        heading=heading[:500],
        embedding=vector,
        covers=covers,
        kind=kind,
        item_id=item_id,
    )


async def _summarise(prompt: str, content: str, fallback: str) -> str:
    """One model call, on a worker thread so a slow provider never blocks the
    event loop, degrading to a real excerpt rather than failing the pass. A
    summary nobody could generate is better skipped than invented — but the
    fallback here is genuine document text, which is the safe wrong answer."""
    from packages.core.llm import chat

    try:
        reply = await asyncio.to_thread(
            chat, [{"role": "user", "content": prompt + content}]
        )
        body = reply.strip()
    except Exception:
        body = ""
    return body or fallback


async def summarize_document(
    session: AsyncSession, scope: Scope, item_id: str, title: str,
    generated_by: str = "ingest",
) -> int:
    """Build a document's section summaries + card. Returns how many were written.

    Sections are the passages grouped by their heading path — the structure the
    chunker already recorded, so no re-parsing. A single-passage section still
    gets a summary: it is a semantic entry point the agent can navigate by.
    """
    passages = await chunks.for_item(session, scope, item_id)
    if not passages:
        return 0

    sections: dict[str, list[Any]] = {}
    for p in passages:
        sections.setdefault(p.heading or title, []).append(p)

    written = 0
    section_notes: list[tuple[str, str]] = []
    for heading, members in list(sections.items())[:MAX_SECTIONS]:
        covers = [m.chunk_id for m in members]
        combined = "\n\n".join(m.text for m in members)
        body = await _summarise(_SECTION_PROMPT, combined, fallback=combined[:400])
        await write_summary(
            session, scope,
            chunk_id=summary_chunk_id("section_summary", covers),
            item_id=item_id, kind="section_summary", heading=heading,
            body=body, covers=covers, generated_by=generated_by,
        )
        section_notes.append((heading, body))
        written += 1

    # The card summarises the section summaries and covers every passage in the
    # document, so opening the card is a way into the whole file.
    all_covers = [p.chunk_id for p in passages]
    card_input = "\n".join(f"- {h}: {b}" for h, b in section_notes)
    card_body = await _summarise(
        _CARD_PROMPT, card_input, fallback=(section_notes[0][1] if section_notes else title)
    )

    # What kind of document this is, what it is about, who it names. Derived
    # from the card that was just written, stored in its OWN column, and
    # appended to the card text.
    #
    # That last part is the whole of the retrieval improvement, and it needed
    # no ranking code: routing already scores documents on their card, and the
    # card is already embedded and searched — so an inferred topic starts
    # helping the moment it is written down. It also cannot become evidence,
    # because chunks.keyword_search has always excluded cards. An answer may be
    # ROUTED by a guess and can never be CITED to one.
    attributes = await derive.describe(card_body)
    if attributes:
        await derive.store(session, scope, item_id, attributes)
        line = derive.as_card_line(attributes)
        if line:
            card_body = f"{card_body}\n\n{line}"

    await write_summary(
        session, scope,
        chunk_id=summary_chunk_id("card", [item_id]),
        item_id=item_id, kind="card", heading=title,
        body=card_body, covers=all_covers, generated_by=generated_by,
    )
    written += 1

    await session.commit()
    return written


__all__ = ["MAX_SECTIONS", "summarize_document", "summary_chunk_id", "write_summary"]
