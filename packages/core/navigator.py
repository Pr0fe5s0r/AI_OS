from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import expand, graph, tree
from packages.core import routing as route
from packages.core.search import RetrievalConfig, Trace, new_trace_id, search_traced
from packages.shared.schema import Hit, Lifecycle, Passage, Scope, SourceRef

# A progress sink: the navigator calls it with one event dict per step when a
# caller wants to watch the loop work. Async so the sink can be an SSE queue.
EmitFn = Callable[[dict[str, Any]], Awaitable[None]]


def _int_env(name: str, default: int) -> int:
    """An operator override that a typo cannot turn into a broken store.

    Zero is allowed through rather than clamped, because for MAX_DOCUMENTS zero
    is a meaningful value — "no count limit" — and not a mistake. Negatives are
    a mistake and become zero, which is the safe reading of "I did not want a
    cap here".
    """
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default

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

# How many documents' outlines go in front of the model at once.
#
# This was 40, and 40 was the wrong KIND of limit. It was a stand-in for "the
# catalogue stops fitting in the prompt" — but the catalogue budget measures
# that directly now, in characters, and shortens to fit. Two guards for one
# problem, and the count-based one bit first and silently: a store of a hundred
# documents showed the agent the forty most recently uploaded and the other
# sixty did not exist as far as the answer was concerned.
#
# Measured on 12-section policy documents, what the model actually receives
# after the budget shortens it:
#
#      10 docs    30,710 ch full  ->  1,470 ch sent
#      40 docs   122,900 ch full  ->  5,940 ch sent
#     100 docs   307,280 ch full  -> 14,880 ch sent      <- still inside 24,000
#     200 docs   614,560 ch full  -> ~24,000 ch sent     <- the budget binds here
#
# So the prompt was never the reason for 40. The real per-document cost outside
# the prompt is parsing its Markdown into a tree on every question, which is
# CPU and grows linearly — that is what this number is for now, and it is set
# where the budget takes over rather than far below it.
#
# Routing still ranks documents (core.routing), so on a store past this the
# ones shown are the ones the question is about, not the newest.
#
# DEFAULT IS 0, MEANING NO COUNT LIMIT. The store itself has never had a size
# limit and this was never one — it bounded how many outlines go into a single
# prompt, which is a different thing that happened to be written as a document
# count. Written as a count it kept being mistaken for a ceiling on the store,
# and every value anyone picked (40, then 250) was arbitrary.
#
# What genuinely cannot be unbounded is the BYTES pulled out of Postgres to
# build those outlines, because every document's full body is loaded and parsed
# on every question. That is bounded below, by MAX_CATALOGUE_BYTES, which is the
# real constraint stated in the units the constraint is actually in: a thousand
# small circulars all fit; one War and Peace at 3.3 MB costs what 3.3 MB costs.
MAX_DOCUMENTS = _int_env("MAX_DOCUMENTS", 0)

# The real bound: how much document text may be loaded to build one catalogue.
#
# 64 MB. Chosen against what a body actually costs — War and Peace is 3.3 MB of
# Markdown, a policy circular is 30–130 KB — so this is roughly twenty novels or
# several thousand ordinary documents in one question's working set, and it
# fails by showing fewer documents rather than by exhausting the container.
#
# Documents are loaded in ranked order, so when the budget runs out what is
# dropped is what routing scored lowest, not whatever the database returned
# last.
MAX_CATALOGUE_BYTES = _int_env("MAX_CATALOGUE_BYTES", 64 * 1024 * 1024)
# Each round is one model call. Enough to open a document, read two or three
# sections and answer; short enough that a model looping on itself stops.
MAX_ROUNDS = 6
# Reading the same section twice is a loop, not research.
MAX_READS = 6
# The most sections one read_section call may ask for. Batching several is the
# point of the list form; asking for a RANGE is not. Left uncapped, the agent
# requested 299 sections in a single call the first time it was offered a list.
MAX_SECTIONS_PER_CALL = MAX_READS
MAX_SECTION_CHARS = 4000
# Looking at a page costs a second model call on a bigger model. Two is enough
# to check a table and the page after it; more than that is the agent browsing.
MAX_LOOKS = 2
# How many passages a single hybrid_search call surfaces to the agent. Enough
# to cover a figure that appears in two or three places, few enough that the
# agent still reads rather than dumps.
MAX_HYBRID_HITS = 4
# How much of the prompt the table of contents may take. Chosen from what real
# collections measure: two documents of 16 sections come to 4,048 characters
# and are unaffected; ten ordinary documents come to 111,858 and are not.
CATALOGUE_BUDGET = 24_000

_SYSTEM = (
    "You answer questions from a document store by navigating it, like a person "
    "who knows where to look.\n\n"
    "How to work:\n"
    "1. You are given every document's title and its table of contents.\n"
    "2. Call read_section on the sections most likely to hold the answer. Judge "
    "by what a section CONTAINS, not by words it shares with the question.\n"
    "3. READ SEVERAL SECTIONS IN THE SAME TURN. Issue three or four "
    "read_section calls at once rather than one, waiting, then another — every "
    "turn is a round trip, and reading three sections in one turn costs the "
    "same wait as reading one. Reading a section that turns out to be "
    "irrelevant costs nothing; missing one loses the answer.\n"
    "4. Do NOT walk a document in order. Chapter I, then II, then III is not "
    "navigation, it is reading the whole book slowly. Pick the sections that "
    "look right, wherever they are.\n"
    "5. If what you read is not enough, read another batch before answering.\n"
    "6. Call submit_answer when you can answer, or when nothing in the store "
    "can.\n\n"
    "A document marked is_a_picture has no text of its own — a photograph, a "
    "screenshot, a scan. Its sections are a DESCRIPTION of the image, written "
    "by looking at it once, so anything precise about it should come from "
    "look_at_page rather than from that description.\n\n"
    "Two things do not survive a PDF's text layer. Tables come out as a run of "
    "numbers with no rows. Figures and diagrams do not come out AT ALL — the "
    "text carries their caption and nothing of what they show. When either is "
    "what the question needs, look at the page rather than guessing, and never "
    "conclude a document lacks something that would live in a picture until "
    "you have looked at one.\n\n"
    "Every section you read is given a number. Cite those numbers in your "
    "answer like [1] or [2][3]. Cite only numbers you were actually given. "
    "Never state anything the sections do not say. If the store does not "
    "answer the question, say so plainly in submit_answer and cite nothing."
)

# The escalation. Not in _TOOLS: it is handed to the model only AFTER it has
# read something, so looking can never be first-line retrieval. Reading is
# cheap, exact and citable; looking is a second model call on a page picture,
# and a system that reaches for it before trying the text is not saving a hard
# question, it is skipping an easy one.
_LOOK_TOOL = {
    "type": "function",
    "function": {
        "name": "look_at_page",
        "description": (
            "Look at a page of the original document as a picture, and read it "
            "with vision instead of extracted text. Two cases:\n"
            "1. The text you read is there but unusable — a table whose rows "
            "and columns collapsed into a run of numbers, a form that lost its "
            "labels, a layout where no value can be tied to its heading.\n"
            "2. The answer is in something the page SHOWS rather than says — a "
            "figure, a diagram, a chart, a screenshot. None of that is in the "
            "text layer, so no amount of reading will find it.\n"
            "Do not use it to find a section: reading is better at that."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc": {"type": "string", "description": "The document id."},
                "page": {"type": "integer", "description": "1-based page number."},
                "looking_for": {
                    "type": "string",
                    "description": (
                        "What to read off the page, e.g. 'the revenue table for "
                        "2024 and 2025'. Guides what is transcribed."
                    ),
                },
            },
            "required": ["doc", "page", "looking_for"],
        },
    },
}

# Appended to the system prompt only in agentic mode, where the agent also has
# hybrid_search. The catalogue tells it where structure lives; hybrid_search is
# for what structure hides — a figure or identifier buried mid-section that no
# heading advertises. It reasons over the outline first and reaches for search
# when a title cannot tell it where a value is.
_HYBRID_NOTE = (
    "\n\nYou also have hybrid_search, which finds passages by meaning and by "
    "exact wording across the whole store. Use the table of contents to reason "
    "about where an answer lives, and hybrid_search when the answer is a "
    "specific figure, name or identifier that no heading would announce. "
    "Passages it returns are numbered exactly like sections you read, and you "
    "cite them the same way."
)

# Appended only when the collection HAS a similarity graph. Split out of the
# note above because describing a tool the agent was not given is an invitation
# to call it — and it would be handed back an empty result, having spent a
# round.
_NEIGHBOURS_NOTE = (
    " When a passage is close but not the whole answer, call neighbors with its "
    "chunk_id to read the passages nearest it in the store's graph — the rest of "
    "the answer often sits one hop away."
)

_HYBRID_TOOL = {
    "type": "function",
    "function": {
        "name": "hybrid_search",
        "description": (
            "Find passages anywhere in the store by meaning and exact wording. "
            "Best for a specific figure, name or identifier that no section "
            "title would announce. Returns passages already numbered for citing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for — a phrase, figure or identifier.",
                }
            },
            "required": ["query"],
        },
    },
}

# The graph hop. Only useful once a hybrid_search has surfaced a passage with a
# real chunk_id — read_section returns tree-node ids, which are not nodes in the
# similarity graph — so it is offered alongside hybrid_search and points the
# model at the chunk_ids that came back from it.
_NEIGHBOURS_TOOL = {
    "type": "function",
    "function": {
        "name": "neighbors",
        "description": (
            "Given the chunk_id of a passage a hybrid_search returned, list the "
            "passages nearest it in the store's own similarity graph. Related "
            "material often sits one hop from the first hit, where a fresh search "
            "would miss it. Returns passages numbered for citing, exactly like "
            "the sections you read, each with its own chunk_id to hop from again."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chunk_id": {
                    "type": "string",
                    "description": "A chunk_id from a hybrid_search result.",
                }
            },
            "required": ["chunk_id"],
        },
    },
}

# Offered only when the catalogue had to be shortened. On a collection that
# fits, this tool does not exist as far as the model is concerned — which keeps
# the small case byte-identical to what it was before the budget existed, so it
# cannot regress.
_OPEN_TOOL = {
    "type": "function",
    "function": {
        "name": "open_document",
        "description": (
            "Show what is inside a document, or inside one part of it. The list "
            "you were given is shortened — top-level headings only, without the "
            "sections nested under them.\n"
            "Give `doc` alone to see that document's top-level parts. Give "
            "`section` as well to see what is inside ONE of those parts, which "
            "is how you reach a chapter in a long document. Reading a whole "
            "top-level part instead is how you run out of rounds."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc": {"type": "string", "description": "The document id."},
                "section": {
                    "type": "string",
                    "description": "Optional section id to look inside, e.g. 'n003'.",
                },
            },
            "required": ["doc"],
        },
    },
}

# What the page reader is asked for. A transcription, not an answer: the
# navigator does the reasoning, and a vision model that both reads AND
# concludes gives you a conclusion with no way back to what was on the page.
_LOOK_PROMPT = (
    # Narrowing this to "only what bears on the question" was measured and
    # rejected. It is three times faster on a dense page — 7.8s to 2.7s — and
    # it cost two of twenty answers: a pentagon came back as "a quadrilateral,
    # four sides", and the periodic table came back as NOT_ON_THIS_PAGE, after
    # which the agent answered from a different document altogether. Whatever
    # a model hears in "report only what is relevant", it is not "be equally
    # careful about less". Buy speed somewhere it cannot cost an answer.
    "Report what this page shows, faithfully and completely, focusing on: "
    "{looking_for}\n\n"
    "Rules:\n"
    "- Tables: reproduce them as Markdown, keeping every row and column "
    "aligned with its own header. The alignment is the point.\n"
    "- Numbers, dates and units: copy exactly. Never round, never convert.\n"
    "- Charts: read each bar, point or segment against the axis and give its "
    "approximate value, saying which is largest and which smallest. Say when a "
    "value is estimated from the axis rather than printed.\n"
    "- Diagrams and shapes: describe what is DRAWN — each shape, how many "
    "sides it has, its colour, its labels, how the shapes are arranged "
    "relative to each other, the direction of any arrows, and any marker such "
    "as the small square that denotes a right angle. None of this is in the "
    "text, so describing it is the whole job.\n"
    "- If what was asked about is encoded by COLOUR, SHADING or POSITION "
    "rather than written down — a legend, a key, a highlight, a category fill "
    "— read the key, then work out which items it applies to and NAME THEM. "
    "Do not stop at describing the key: 'liquids are shown in blue' answers "
    "nothing without the list of which items are blue. That mapping is "
    "precisely what the text could not tell us, and why this page is being "
    "looked at instead of read.\n"
    "- Never abbreviate with '...' or 'and so on'. A row you skip is a fact "
    "the answer cannot use.\n"
    "- Do not answer the question or draw conclusions. Report what is there.\n"
    "- Reply NOT_ON_THIS_PAGE only if this page has nothing to do with what "
    "was asked — the wrong page, or the wrong document. Information that IS "
    "here but is not spelled out in words — because it is carried by a colour, "
    "a legend, a position, a shape — is on the page. Read it and report it. "
    "Saying NOT_ON_THIS_PAGE about something you can see but that is not "
    "written down defeats the entire purpose of looking.\n\n"
    "Finally, say WHERE on the page you read it. After your report, add up to "
    "four lines in exactly this form and nothing else:\n"
    "REGION x=<left> y=<top> w=<width> h=<height> | <what is there>\n"
    "measured as percentages of the page, 0 to 100, from the top-left corner. "
    "Mark only the parts that actually answer what was asked — the cell, the "
    "row, the bar, the legend entry — not the whole page. A reader is shown "
    "these boxes drawn on the page, so a box round everything tells them "
    "nothing and a box round the wrong thing is worse than none at all. If you "
    "cannot place something confidently, leave it out."
)
_NOT_ON_PAGE = "NOT_ON_THIS_PAGE"

# An answer that concludes the store does not say something.
#
# Needed because a negative can arrive two ways and only one of them is
# honest. submit_answer(found=false) says so outright. But the model also
# writes a confident paragraph explaining what is NOT there — "the owl is not
# named", "the text does not give" — while reporting found=true, because from
# its point of view it did answer the question it was asked.
#
# Both are conclusions of absence, and a conclusion of absence drawn from two
# sections that a search picked is worth almost nothing. This is what triggers
# one push to search before such an answer is accepted.
_READS_AS_ABSENT = re.compile(
    r"\b(?:is|are|was|were)\s+not\s+(?:named|given|provided|specified|mentioned|stated)\b"
    r"|\bdoes\s+not\s+(?:say|name|give|provide|specify|mention|state)\b"
    r"|\bno\s+(?:mention|name|reference)\s+of\b"
    r"|\bnot\s+(?:provided|specified|named)\s+in\s+the\b"
    r"|\bcannot\s+be\s+determined\b",
    re.I,
)


def _reads_as_absent(text: str) -> bool:
    return bool(_READS_AS_ABSENT.search(text or ""))


def _should_search_first(
    *, absent: bool, hybrid: bool, searched: bool, pressed: bool, rounds_left: bool
) -> bool:
    """Whether to send the agent back to search before accepting a negative.

    Every condition has to hold, which is what keeps this off the fast path:
    it costs a round ONLY when the agent is about to say something is not there
    and has not actually looked for it.

    The case it exists for: asked "Harry owl name", the index hint matched two
    pages that mention owls and name none of them. The agent read exactly those
    two, concluded "the owl is not named", and reported found=true — in 8.5
    seconds, having never once searched for "Hedwig". The hint is chosen by
    searching the whole QUESTION; the answer is often a single word inside it,
    and those are not the same query.
    """
    return absent and hybrid and not searched and not pressed and rounds_left


_SEARCH_FIRST = (
    "Before concluding that. The sections you read were chosen by searching "
    "your whole question, which is not the same as searching for the thing the "
    "question ASKS FOR — a name, a number, a title. You have not run "
    "hybrid_search once.\n"
    "Run it now on the specific term itself, not the question: if you are asked "
    "what something is called, search for what you think it might be called, or "
    "for the words that would appear beside the name. Then answer.\n"
    "If the search also finds nothing, say so and cite what you read — a "
    "considered 'not in these documents' is a fine answer. A guess after two "
    "sections is not."
)

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_section",
            "description": (
                "Read the full text of one or more sections of a document. "
                "Pass SEVERAL section ids at once whenever you have more than "
                "one candidate — they are read in a single step, and reading "
                "three costs the same wait as reading one. Returns each "
                "section's text with a citation number to use in your answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doc": {"type": "string", "description": "The document id."},
                    "section": {
                        # Either shape is accepted. A list is what the tool is
                        # for, but a schema that ONLY took a list would make a
                        # single read look like the unusual case, and models
                        # reliably send a bare string anyway — refusing it would
                        # cost a round to discover.
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": (
                            "One section id (e.g. 'n014') or several "
                            f"(e.g. ['n014','n015','n016']), at most "
                            f"{MAX_SECTIONS_PER_CALL}. Prefer several — but "
                            "pick the likely ones, do not ask for a range."
                        ),
                    },
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


async def _documents(
    session: AsyncSession,
    scope: Scope,
    limit: int = MAX_DOCUMENTS,
    item_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The documents to lay out for the agent.

    ``item_ids`` names them explicitly — from an explicit filter, or from the
    router having chosen. The order given is preserved, because it is a ranking:
    a router that ranks and then hands back an arbitrary order has thrown away
    the half of its work that says which document is most likely.

    Without it, the newest ``limit`` documents. That default is only ever
    correct when the store holds no more than ``limit``; see core.routing for
    why, and for what happens when it holds more.

    ``limit`` of 0 means no count limit, which is the default. The bound that
    always applies is MAX_CATALOGUE_BYTES, enforced after the rows come back:
    documents are taken in order until the body budget is spent. Ordered by
    rank when routing chose them, so what falls off the end is what scored
    lowest rather than whatever the database happened to return last.
    """
    clause = "AND collection_id = :c" if scope.collection_id else ""
    params: dict[str, Any] = {
        "w": scope.workspace_id,
        "c": scope.collection_id,
        "active": str(Lifecycle.ACTIVE),
        # SQL has no "no limit" parameter, so 0 becomes a number no store will
        # reach. The real guard is the byte budget below.
        "limit": limit if limit > 0 else 1_000_000,
    }
    if item_ids:
        params["ids"] = list(item_ids)
        rows = (
            await session.execute(
                sql(
                    f"""
                    SELECT item_id, title, body, source, locator, url
                    FROM kb_items
                    WHERE workspace_id = :w AND status = :active {clause}
                      AND item_id = ANY(:ids)
                    LIMIT :limit
                    """  # noqa: S608 - clause is a fixed literal, not input
                ),
                params,
            )
        ).all()
        # Restored to the caller's order — SQL returned a set, not a ranking.
        rank = {item_id: n for n, item_id in enumerate(item_ids)}
        rows = sorted(rows, key=lambda r: rank.get(r.item_id, len(rank)))
    else:
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
                params,
            )
        ).all()
    # The bound that actually protects the process. Stated in bytes because
    # that is the unit the cost is in: a thousand 30 KB circulars are cheaper
    # to lay out than ten novels, and any limit written as a document count
    # gets one of those two cases badly wrong.
    #
    # The first document is always taken, whatever it weighs. A store whose
    # single document is larger than the budget must still be answerable from
    # it — returning nothing would report an empty collection.
    out: list[dict[str, Any]] = []
    spent = 0
    for r in rows:
        body = r.body or ""
        if out and spent + len(body) > MAX_CATALOGUE_BYTES:
            break
        spent += len(body)
        out.append(
            {
                "item_id": r.item_id,
                "title": r.title,
                "body": body,
                "source": r.source,
                "locator": r.locator,
                "url": r.url,
            }
        )
    return out


# Words that name a thing a page SHOWS rather than says. A question using one
# of them is asking about content the text layer provably does not hold: a PDF
# keeps a figure's caption and none of its contents.
#
# Deliberately short. "Graph" and "plot" are missing because they mean other
# things in ordinary use — a graph database, the plot of a report — and a word
# that fires on the wrong questions would spend a vision call on every one of
# them. "Table" is missing for the opposite reason: table text usually DOES
# survive extraction, mangled but present, and the existing garbled-text path
# already covers it.
_PICTURE_WORDS = re.compile(
    # "fig." is matched on its own: a trailing word boundary cannot hold after
    # a full stop, so folding it into the plural group silently never matched.
    r"\bfig\.|\b(?:figure|diagram|chart|screenshot|illustration|image|photo)s?\b",
    re.I,
)


def _is_a_picture(document: dict[str, Any]) -> bool:
    """Is this document itself a picture, rather than a document with pictures?

    A PDF has text of its own and pages that can be looked at. An image has
    nothing but the picture, so everything indexed for it is a description —
    and a description is the wrong thing to answer a precise question from.
    """
    from packages.core import pages

    return pages.is_image("", document.get("locator") or "")


# Above this many characters, what was indexed for an image is a READING of
# it, not a caption. Measured: the readiness-overlay diagram indexed at 3,630
# characters, ending in an explicit "Color Coding and Legend Mapping" section
# that named every green, orange and red component. The thin case this guard
# was written for — a photo whose description is one line — falls far below.
TRANSCRIBED_ENOUGH = 1_200


def _already_transcribed(document: dict[str, Any]) -> bool:
    """Has this picture's own content already been read into its text?

    The press that follows exists because a PDF's text layer holds a figure's
    caption but not what the figure SHOWS, so answering from prose alone means
    answering about a picture nobody looked at. For an image document that
    premise is simply false: there is no text layer, and everything indexed
    for it came out of the vision model reading the whole picture at ingest.

    Forcing a second look there re-derives what is already in the body, at the
    price of a vision call on the request path. Measured on a six-document
    store: 65.5s of a 77.1s walk, and 28.7s of a 33.9s one, both spent
    re-reading a diagram whose transcription already carried the answer
    verbatim. The same question with no picture involved took 3.4s.

    The model may still CHOOSE to look — the tool stays offered, and a
    question-directed look can pull detail a general transcription missed.
    What stops is the machine forcing one it does not need.
    """
    return len(document.get("body") or "") >= TRANSCRIBED_ENOUGH


def _has_pictures(document: dict[str, Any]) -> bool:
    """Can a page of this document be shown as a picture?

    PDFs and images can. A deck writes the same `<!-- page N -->` markers a PDF
    does — deliberately, so slides index and cite like pages — which made it
    look renderable when nothing can render it without a headless Office.
    """
    from packages.core import pages

    return pages.renderable("", document.get("locator") or "")


def _about_a_picture(question: str) -> bool:
    """Is this question asking about something only a picture can answer?"""
    return bool(_PICTURE_WORDS.search(question))


_PSEUDO_CALL = re.compile(r"\n*\s*(submit_answer|read_section|look_at_page)\s*\(", re.I)


def _strip_pseudo_call(text: str) -> str:
    """Cut a tool call the model typed out as prose instead of calling.

    Some models finish a good answer and then append `submit_answer(answer=...)`
    as literal text. The answer above it is real and worth keeping; the typed
    call is machinery leaking onto the reader's screen, and it appears verbatim
    in the console under a heading that says this is what the store found.
    """
    match = _PSEUDO_CALL.search(text)
    return text[: match.start()].rstrip() if match else text


# How much caller-supplied style to accept. Long enough for a real house
# style, short enough that a document cannot be pasted in as "behaviour" and
# become instructions.
MAX_BEHAVIOUR_CHARS = 600


def _behaviour_note(behaviour: str) -> str:
    """Caller-supplied style, fenced so it cannot become caller-supplied truth.

    Being able to say "answer in bullet points" or "write for a non-technical
    reader" is the difference between a demo and something a team can fit to
    its own house style. But the same field would happily accept "you do not
    need to cite anything" or "if the documents are unclear, use your general
    knowledge" — and those are not style, they are the two rules that make an
    answer from this store worth more than an answer from anywhere else.

    So it goes in LAST, clearly marked as being about form, and followed by a
    line restoring precedence. Bounded too: a field long enough to hold a
    document is a field someone will paste a document into.
    """
    text = " ".join((behaviour or "").split())[:MAX_BEHAVIOUR_CHARS]
    if not text:
        return ""
    return (
        "\n\nHOW TO WRITE THE ANSWER (the caller's preference, about STYLE "
        "only):\n"
        f"{text}\n"
        "That preference governs tone, length and formatting. It does not "
        "change what you may say: cite only sections you actually read, and if "
        "they do not answer the question, say so plainly. A style asking for "
        "confidence or brevity is never a reason to state something the "
        "documents do not."
    )


# The submit_answer PARAMETER name, written out as a label on its own line.
_ANSWER_LABEL = re.compile(r"^[ \t]*answer[ \t]*:[ \t]*", re.I | re.M)

# How much text may follow that label before it stops looking like a summary of
# what was already said. Measured leak: 900 characters of finished answer, then
# "answer: The two surfaces of the user experience are the Agent page and the
# Feed page." — 88 characters restating it.
RESTATEMENT_CHARS = 400


def _strip_answer_label(text: str) -> str:
    """Cut the ``answer:`` label a model writes when it means to call the tool.

    A sibling of _strip_pseudo_call and not covered by it: that one needs an
    opening bracket to fire, and this leak has none. The model finishes a good
    answer in prose and then appends the submit_answer parameter name with a
    one-line restatement after it, which reaches the reader verbatim under a
    heading saying this is what the store found.

    Cutting text is riskier than leaving it, so the two shapes are treated
    differently and neither can lose an answer:

      label first   The whole reply is ``answer: <the answer>``. Only the label
                    goes; every word after it is the answer itself.

      label last    Something complete was already said. What follows is a
                    restatement, so it goes — but only when it is short enough
                    to BE one. A document quoting "Answer:" in an FAQ, or a
                    model genuinely continuing, runs long and is left alone;
                    truncating a real answer is a correctness failure, while
                    leaving a duplicate line is untidy.
    """
    match = _ANSWER_LABEL.search(text)
    if not match:
        return text
    before = text[: match.start()].rstrip()
    if not before:
        return text[match.end() :].lstrip()
    after = text[match.end() :].strip()
    return before if len(after) <= RESTATEMENT_CHARS else text


_REGION = re.compile(
    r"^\s*REGION\s+x=(-?[\d.]+)\s+y=(-?[\d.]+)\s+w=(-?[\d.]+)\s+h=(-?[\d.]+)\s*\|?\s*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)


def _regions_in(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Pull the "where I read it" lines out of a page reading.

    Returned separately from the prose because they are for DRAWING, not for
    reading: the reader sees boxes on the page, not coordinates in a sentence.
    Leaving them in the passage would put "REGION x=12 y=44" into an answer's
    evidence, which is machinery on the reader's screen again.

    Anything outside the page, inverted, or big enough to be "the whole page"
    is dropped. A box round everything tells nobody anything, and a box round
    the wrong thing is worse than no box: it is a confident pointer at the
    wrong evidence.
    """
    found: list[dict[str, Any]] = []
    for match in _REGION.finditer(text):
        try:
            x, y, w, h = (float(match.group(i)) for i in range(1, 5))
        except ValueError:
            continue
        label = match.group(5).strip().strip("|").strip()
        if w <= 0 or h <= 0:
            continue
        # Clamp to the page rather than discard: a model that says the row runs
        # to 102% has still pointed at the right row.
        x, y = max(0.0, min(x, 100.0)), max(0.0, min(y, 100.0))
        w, h = min(w, 100.0 - x), min(h, 100.0 - y)
        if w * h >= 8000:  # 80% of the page in both directions
            continue
        found.append(
            {"x": round(x, 2), "y": round(y, 2), "w": round(w, 2), "h": round(h, 2), "label": label[:120]}
        )
        if len(found) >= 4:
            break

    return _REGION.sub("", text).strip(), found


def _stream_round(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    on_token: Callable[[str], None],
) -> dict[str, Any]:
    """One round, with its prose handed over as it arrives.

    Returns exactly what the non-streaming call returns, so the loop above does
    not branch on how the round was fetched — only on what came back.

    Tokens from a round that turns out NOT to be the answer are still sent:
    there is no way to know in advance, since a round becomes an answer only by
    ending without a tool call. The caller is told to discard what it has shown
    when that happens, which is why a reset exists at all.
    """
    from packages.core.llm import stream_chat_with_tools

    done: dict[str, Any] = {"content": "", "tool_calls": []}
    for event in stream_chat_with_tools(messages, tools, temperature=0.0):
        if event["type"] == "text":
            on_token(event["delta"])
        elif event["type"] == "done":
            done = {"content": event["content"], "tool_calls": event["tool_calls"]}
    return done


# Readings already paid for. Keyed by the page AND by what was asked of it,
# because the reading is steered: a page read for "the revenue table" is not
# the answer to "which vertex has the right angle", and serving one for the
# other would trade the most expensive call in the system for a wrong answer.
# That makes this a cache for repeats — the same question asked twice, the same
# question in both retrieval modes — and nothing else, which is the only
# version of it that cannot cost accuracy.
_READ_CACHE: OrderedDict[tuple[str, str, int, str], str] = OrderedDict()
_READ_CACHE_SIZE = 256


async def _read_page(
    workspace_id: str, item_id: str, page: int, looking_for: str
) -> str | None:
    """A page of the original, read with vision. None if that is not possible.

    None rather than an exception on every failure path — no object store, no
    vision model, a file that will not render, a provider that is down. Every
    one of them means the same thing to the caller: carry on with the text.
    Escalation that can take the whole answer down with it is worse than no
    escalation.
    """
    from packages.core import pages

    if not pages.available():
        return None
    png = await pages.image(workspace_id, item_id, page)
    if png is None:
        return None

    key = (workspace_id, item_id, page, " ".join(looking_for.lower().split()))
    cached = _READ_CACHE.get(key)
    if cached is not None:
        # Reading the page again would cost the most expensive call this
        # system makes to produce a string it already has.
        _READ_CACHE.move_to_end(key)
        return cached

    from packages.core.llm import look

    try:
        seen = await asyncio.to_thread(
            look,
            png,
            _LOOK_PROMPT.format(looking_for=looking_for),
            model=pages.vision_model(),
        )
    except Exception:
        return None

    reading = (seen or "").strip() or None
    # A refusal is the one answer that must never be cached. NOT_ON_THIS_PAGE
    # is the model failing, not a fact about the page — measured at 0 out of 5
    # on a page that had once refused — and caching it would pin that failure
    # to the page for the life of the process, so the same question could never
    # recover. Cache readings; retry refusals.
    if reading is not None and not reading.startswith(_NOT_ON_PAGE):
        _READ_CACHE[key] = reading
        while len(_READ_CACHE) > _READ_CACHE_SIZE:
            _READ_CACHE.popitem(last=False)
    return reading


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


_NAMED = re.compile(
    r"\b(figure|fig\.|table|chart|exhibit|appendix)\s*([0-9]+[A-Za-z]?)\b", re.I
)


def _where_named_things_live(
    question: str, documents: list[dict[str, Any]], trees: dict[str, tree.Node]
) -> str:
    """Point at the exact section holding each figure the question names.

    With one document the agent finds a figure by reading titles. With three it
    guessed: asked about Figure 9 — which exists only in the Operations Review
    — it read pages 3 and 4 of the Attention paper and then looked at two of
    ITS pages, found neither, and reported the store could not say. Every
    document has a page 3; only one of them has Figure 9.

    The index already knows the answer, because each section lists the captions
    it contains. Saying it outright is not a hint or a guess — it is a lookup,
    and leaving the agent to re-derive it from previews is how a search goes
    looking in the wrong book.
    """
    wanted = {
        f"{kind.rstrip('.').title()} {number}" for kind, number in _NAMED.findall(question)
    }
    if not wanted:
        return ""

    lines: list[str] = []
    for doc in documents:
        for node in trees[doc["item_id"]].walk():
            for caption in node.captions():
                if caption in wanted:
                    lines.append(
                        f"- {caption} is in document {doc['item_id']} "
                        f"({doc['title'][:60]}), section {node.node_id} ({node.title})"
                    )
    if not lines:
        return ""
    return (
        "WHERE THE THINGS YOU ASKED ABOUT LIVE (from the index, not a guess):\n"
        + "\n".join(lines)
        + "\n\n"
    )


def _why_these_documents(
    routing: route.Routing, documents: list[dict[str, Any]]
) -> str:
    by_title = {doc["item_id"]: doc["title"] for doc in documents}
    """Tell the agent this list was narrowed, and on what evidence.

    Two failures this prevents, both seen before it existed.

    A narrowed list looks exactly like a small store. Given eight documents out
    of two hundred and no word about it, an agent that fails to find the answer
    concludes the STORE does not hold it — and says so, confidently, which is
    the one sentence a knowledge base must not get wrong. It has to know it is
    looking through a window.

    The other is subtler: told only that the list was narrowed, the agent starts
    treating the top-ranked document as the answer and stops reading. So the
    reasons are given per document and named as what they are — a similarity
    score, not a finding. Ranking says where to look first, never what is true.
    """
    if routing.how == "everything":
        return ""
    if routing.how == "ranked":
        # Nothing was excluded — every document is in the catalogue above. This
        # says which ones the index believes the question is about, so the agent
        # starts there instead of sampling whatever the search returned.
        #
        # The case this exists for: asked "server requirements" of a store
        # holding one server document and an 11,460-passage Harry Potter
        # collection, the agent searched the novel, read two pages of it, and
        # then explained in its answer that Harry Potter is unrelated to server
        # requirements. The cards already knew — 0.778 against 0.614 — and
        # nobody had told it.
        lines = [
            f"- {item_id} ({by_title.get(item_id, '')[:60]}): "
            f"{routing.because.get(item_id, 'matched the question')}"
            for item_id in routing.order
            if item_id in by_title
        ]
        return (
            "WHICH DOCUMENTS THIS QUESTION LOOKS LIKE IT IS ABOUT, best match "
            "first, from the index — each document's summary and its passages "
            "scored against the question. Start with the first one. Nothing is "
            "excluded and you may read any document above; this is a ranking, "
            "not a filter, and a ranking is not a finding.\n"
            "Do NOT read from a document far down this list to 'check' it. If "
            "the top one answers the question, answer.\n" + "\n".join(lines) + "\n\n"
        )
    if routing.how == "named":
        return (
            "THESE DOCUMENTS WERE NAMED IN THE REQUEST. Answer only from them. "
            "If they do not contain the answer, say so — do not reason about "
            "documents you were not given.\n\n"
        )

    lines = [
        f"- {doc['item_id']} ({doc['title'][:60]}): "
        f"{routing.because.get(doc['item_id'], 'matched the question')}"
        for doc in documents
    ]
    return (
        f"HOW THIS LIST WAS CHOSEN: the store holds {routing.available} documents, "
        f"too many to lay out at once, so these {len(documents)} were selected by "
        "matching the question against the index. That is a RANKING, not a "
        "finding — it says where to look first, and nothing about what is true. "
        "Read and judge for yourself, and if the answer is not in these, say it "
        "was not found in the documents you were given rather than that the "
        "store does not hold it.\n" + "\n".join(lines) + "\n\n"
    )


# What a page is asked for when the steered ask came back empty. Deliberately
# has no subject in it: the whole point is to stop narrowing.
_UNSTEERED = "everything on this page"


def _only_page_of_a_picture(document: dict[str, Any] | None, total_pages: int) -> bool:
    """Is this page the entire document, and is that document a picture?

    The narrow case where a refusal cannot mean "wrong page" — there is no other
    page. It can only mean the reader declined, and a reader that declines is
    worth asking twice. On a forty-page PDF a refusal is genuine information
    (this page, not that one) and re-asking would just spend the look again.
    """
    return document is not None and total_pages <= 1 and _is_a_picture(document)


def _node_holding(root: tree.Node, text: str) -> tree.Node | None:
    """The deepest section whose text contains this passage.

    Located by content, not by heading. A novel has thirty-four chapters called
    "Chapter I", so a title is not an address — and the whole point of this
    lookup is to hand back something the agent can actually read_section on.
    """
    probe = " ".join(text.split())[:120]
    if len(probe) < 24:
        return None
    best: tree.Node | None = None
    for node in root.walk():
        if probe in " ".join(node.text.split()):
            if best is None or node.level > best.level:
                best = node
    return best


# How long the section-address hint may hold up the answer.
#
# 8s was too tight and failed silently, which is the worst combination. The hint
# is an embedding call plus a hybrid search: 2.6s warm, but the embedding alone
# has been measured at 4.4s and 5.8s cold, and past the deadline the hint came
# back empty with nothing said. The agent then had no section ids, so it fell
# back to open_document — which on a 3,604-section book is the single largest
# thing in the conversation, and the query never finished.
#
# 25s. Still bounded — the original bug was an UNBOUNDED await, and that is what
# must never come back — but no longer tight enough to lose the hint on a slow
# embedding and turn a fast walk into a 113-second one.
HINT_DEADLINE_SECONDS = 25.0


async def _hint_or_nothing(task: asyncio.Future[str]) -> str:
    """Wait for the hint, but never on it.

    ``await task`` with no bound is what turned a slow hint into a dead answer.
    Symptom: the SSE stream opened, reported the index, emitted nothing further,
    and the browser eventually gave up with ERR_INCOMPLETE_CHUNKED_ENCODING —
    "network error" on screen. Nothing in any log, because nothing had failed;
    the walk was simply parked on an await that never returned.

    The hint is an optimisation and its own docstring already says a hint that
    cannot be produced is not an error. That has to be true of a hint that is
    merely slow as well, or the optimisation becomes a dependency. Losing it
    costs a slightly worse prompt; waiting on it costs the answer.
    """
    try:
        # wait_for cancels the task itself on timeout, so nothing is left
        # running against a database session nobody is reading from.
        return await asyncio.wait_for(task, HINT_DEADLINE_SECONDS)
    except Exception:
        return ""


async def _words_in_parallel(
    scope: Scope, question: str, trees: dict[str, tree.Node], prefer: tuple[str, ...] = ()
) -> str:
    """``_where_the_words_are`` on its own database session, so it can be
    started early and awaited late.

    Its own session because two coroutines sharing one would interleave
    statements on a connection that assumes it is used by one caller at a time.
    Failure is swallowed for the same reason it is inside the hint itself: a
    hint that cannot be produced is not an error, it is one fewer hint.
    """
    from packages.core.db import Session

    try:
        async with Session() as session:
            return await _where_the_words_are(session, scope, question, trees, prefer=prefer)
    except Exception:
        return ""


async def _where_the_words_are(
    session: AsyncSession,
    scope: Scope,
    question: str,
    trees: dict[str, tree.Node],
    limit: int = 6,
    prefer: tuple[str, ...] = (),
) -> str:
    """Sections the index already matched, named so they can be opened.

    Only used when the catalogue had to be shortened, and it exists because of
    what a shortened catalogue costs. Choosing a section means reading its
    title, and in a novel every title is "Chapter XIX" — no amount of structure
    helps, because the author never labelled where anything is. Asked which
    peasant Pierre meets in captivity, the agent drilled correctly into the
    right book, read six chapters by guessing at numerals, and ran out.

    The store already knows: the same embeddings and keyword index hybrid uses
    to answer that question in three seconds. So on a corpus too large to lay
    out, the search runs first and its hits are handed over as addresses. The
    agent still decides what to read and still reads it for itself — this
    replaces guesswork about titles, not the reading.
    """
    from packages.core.search import RetrievalConfig, search

    try:
        # Scoped to the laid-out documents for the same reason the search tool
        # is: a hint pointing at a document the agent was not given is not a
        # hint, it is a dead end it will spend a round on.
        hits = await search(
            session,
            scope,
            question,
            # Expansion belongs HERE, of all the places it could go.
            #
            # This hint decides which sections the agent reads first, and that
            # single decision is what the terse-query failure turns on: with
            # expansion off everywhere, "Harry owl name" walked to two
            # sections, never reached the one naming her, and answered that
            # the owl has no name — while "What is the name of Harry Potter's
            # owl?" answered Hedwig in 3.41s off the same store. The hint
            # needs the word "Hedwig" to point anywhere useful, and only
            # expansion can supply it.
            #
            # It was turned off here once, for a real reason: expanding
            # "Server requirement" to "hardware, specification" pulled a novel
            # into a server question and agentic went 7s -> 88s. Two things
            # have changed since that measurement. The hint now emits a
            # ready-made call for the BEST-RANKED document only rather than
            # every document the words appear in, so a stray term can no
            # longer drag an unrelated document into the walk; and the search
            # the agent runs for itself, below, stays unexpanded, which is
            # where most of that 88s actually came from.
            RetrievalConfig(
                limit=limit,
                item_ids=tuple(trees),
                expand_query=expand.enabled_in_navigator(),
            ),
        )
    except Exception:
        # A hint that cannot be produced is not an error. The agent navigates
        # the way it did before.
        return ""

    # Ordered by the card ranking, not by passage score.
    #
    # The two signals disagree, and the passage arm is the weaker one. Asked
    # "Server requirement" of a store holding one server document and a Harry
    # Potter collection, the passage arm matched "the door to the Room of
    # Requirement" and the hint duly told the agent to read two chapters of the
    # novel. The ranking already knew better — 0.778 against 0.614 — and the
    # agent was left holding two pieces of advice that pointed different ways.
    # It hedged by calling open_document, which is exactly the round this hint
    # exists to save.
    #
    # Ordering, not filtering: every matched section is still offered, because a
    # card is a summary and can be wrong about a detail buried in a document.
    # The best-ranked document simply goes first, and the ready-made call leads
    # with it.
    if prefer:
        rank = {item_id: n for n, item_id in enumerate(prefer)}
        hits = sorted(hits, key=lambda h: rank.get(h.item_id, len(rank)))

    lines: list[str] = []
    # Section ids grouped by the document they belong to, so the hint can end
    # with the exact call to make rather than parts the model has to assemble.
    found: OrderedDict[str, list[str]] = OrderedDict()
    for hit in hits:
        root = trees.get(hit.item_id)
        if root is None:
            continue
        for passage in hit.passages[:2]:
            node = _node_holding(root, passage.text)
            if node is None:
                continue
            line = (
                f"- {hit.item_id} section {node.node_id} ({node.title[:60]}) "
                f"— matched on: {' '.join(passage.text.split())[:90]}…"
            )
            if line not in lines:
                lines.append(line)
                ids = found.setdefault(hit.item_id, [])
                if node.node_id not in ids:
                    ids.append(node.node_id)
        if len(lines) >= limit:
            break

    if not lines:
        return ""

    # The exact call, written out. The hint named the right sections and the
    # agent still spent a round on open_document first — roughly ten seconds to
    # fetch an outline whose relevant entries were already in the prompt. A tool
    # call it can copy removes the step where it decides how to begin.
    # The ready-made call names the BEST-RANKED document only.
    #
    # Listing every matched document here undid the ranking. Asked "Server
    # requirement", the passage arm matched "the door to the Room of
    # Requirement" in a novel; with open_document withheld the agent obediently
    # read every id in this block, and a server-sizing answer cited Harry
    # Potter — the exact contamination the ranking exists to prevent.
    #
    # The other documents stay in the list above, so nothing is hidden and the
    # agent can still reach them if the first does not answer. They are simply
    # not what it is told to start with.
    best = next(iter(found.items()))
    ready = f'  read_section(doc="{best[0]}", section={best[1][:limit]!r})'
    others = len(found) - 1
    return (
        "SECTIONS THE INDEX ALREADY MATCHED TO THIS QUESTION. Read these FIRST, "
        "in one turn, before opening anything else — they were found by "
        "searching the full text by meaning and by exact wording, which is a "
        "far better guide than a chapter title. They are candidates, not the "
        "answer: read them and judge. If they do not settle it, then explore "
        "the contents.\n"
        # The previous wording — "a starting point, not the answer, and not
        # necessarily complete" — was accurate and useless. Measured: the hint
        # named the exact section holding the answer, and the agent ignored it,
        # opened Book One and read Chapter I, II, III, IV, V, VI in order, one
        # per round. Honest hedging that reads as "this is unreliable" gets the
        # evidence thrown away. The hedge is still here; it no longer leads.
        + "\n".join(lines[:limit])
        + "\nThese ids are read_section ids: you do NOT need open_document to "
        "reach them. Start with exactly this, and answer from it if it "
        "settles the question:\n" + ready + "\n"
        + (
            f"(Sections in {others} other document"
            f"{'s' if others != 1 else ''} are listed above as well. They "
            "matched on wording and are usually a different subject — only "
            "read them if the call above does not answer the question.)\n"
            if others
            else ""
        )
        + "\n"
    )


def _entry(doc: dict[str, Any], sections: list[dict[str, Any]]) -> dict[str, Any]:
    """One document's line in the catalogue.

    When index summaries exist for a document (a card and section summaries
    generated at ingest or by the mapper), they are injected here: a ``blurb``
    on the document and a ``summary`` on each section. The agent reads these to
    decide where to look, rather than guessing from headings alone. When a
    document has no summaries yet, the bare outline is still present — the
    agent navigates as before, and the mapper will catch up in the background.

    The summary data was pre-fetched and attached to each document dict by the
    caller (navigate) — see the ``_enrich_catalogue`` call there. Absent, the
    fields are simply omitted and the fallback is the plain outline.

    Injected at THIS level, not in _catalogue, so the blurbs survive the budget:
    a shortened catalogue loses openings and nesting, and a one-line description
    of what a document is about is worth more per character than either.
    """
    entry: dict[str, Any] = {
        "doc": doc["item_id"],
        "title": doc["title"],
        # A picture has no text of its own: what follows is a description of
        # it, written by looking. Saying so is what lets the agent go straight
        # to the page instead of reading a paraphrase and then being sent back
        # for it.
        **({"is_a_picture": True} if _is_a_picture(doc) else {}),
        "sections": sections,
    }
    if doc.get("_card"):
        entry["blurb"] = doc["_card"]
    if doc.get("_section_summaries"):
        by_heading: dict[str, str] = {
            s["heading"]: s["body"]
            for s in doc["_section_summaries"]
            if s.get("heading") and s.get("body")
        }
        for section in entry["sections"]:
            desc = by_heading.get(section.get("title", ""))
            if desc:
                section["summary"] = desc
    return entry


def _headings_only(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Top-level titles, no openings and no children.

    What survives when a document is too big to lay out in full: enough to
    choose a part of it, and nothing more.
    """
    trimmed = []
    for section in sections:
        entry: dict[str, Any] = {"id": section["id"], "title": section["title"]}
        inside = section.get("sections") or []
        if inside:
            entry["contains"] = len(inside)
        trimmed.append(entry)
    return trimmed


def _catalogue(
    documents: list[dict[str, Any]], trees: dict[str, tree.Node], budget: int = CATALOGUE_BUDGET
) -> tuple[str, bool]:
    """Every document and its sections. Titles and openings, never full text.

    Returns the catalogue and whether it had to be shortened.

    This grew without a limit and it was the single worst thing in the system
    at scale. MAX_DOCUMENTS caps how many documents go in front of the model;
    nothing capped how many SECTIONS did, and the reasoning behind that cap —
    "choosing from a list nobody can read is guesswork wearing a suit" — is
    exactly as true of sections. Measured: two copies of one novel came to
    1,491 sections and **270,062 characters, about 67,500 tokens, sent again on
    every round of every question**. A collection of ten ordinary documents
    came to 28,000 tokens.

    Under the budget nothing changes — the same bytes as before, so a small
    collection cannot regress. Over it, each document keeps its top-level
    headings and loses its openings, and the agent is given open_document to
    fetch any one outline in full. A table of contents, then the chapter list
    for the book you actually want.
    """
    full = [_entry(doc, trees[doc["item_id"]].outline().get("sections", [])) for doc in documents]
    rendered = json.dumps(full, ensure_ascii=False)
    if len(rendered) <= budget:
        return rendered, False

    short = [
        _entry(doc, _headings_only(trees[doc["item_id"]].outline().get("sections", [])))
        for doc in documents
    ]
    for entry, doc in zip(short, documents, strict=True):
        entry["sections_in_full"] = tree.count(trees[doc["item_id"]])

    rendered = json.dumps(short, ensure_ascii=False)
    if len(rendered) <= budget:
        return rendered, True

    # Headings alone can still overflow: a novel whose 730 chapters sit at the
    # top level has no shallower shape to fall back on. Each document keeps an
    # equal share of what is left and says how many it is not showing, so the
    # list stays a fair sample of the whole rather than an alphabetical prefix
    # that quietly stops at chapter forty.
    share = max(1, budget // max(len(short), 1) // 90)
    for entry in short:
        sections = entry["sections"]
        if len(sections) > share:
            entry["sections"] = sections[:share]
            entry["headings_not_shown"] = len(sections) - share
    return json.dumps(short, ensure_ascii=False), True


def _outline_of(
    document: dict[str, Any], root: tree.Node, budget: int, section_id: str = ""
) -> str:
    """What is inside a document, or inside one part of it.

    ``section_id`` is what makes this a drill-down rather than a second look at
    the same thing. Without it the first version handed back the top-level
    headings the catalogue had already shown — measured: asked who Natasha
    elopes with, the agent opened the novel, got its 34 book titles again, then
    read five whole BOOKS looking for a scene and ran out of rounds. It could
    see the shelf and never the chapters.

    So a part can be opened too, and one part of a document is small enough to
    lay out properly: titles, openings, and what each contains.
    """
    if section_id:
        found = tree.find(root, [section_id])
        if not found:
            return json.dumps({"error": "no section with that id", "doc": document["item_id"]})
        node = found[0]
        inside = node.outline().get("sections", [])
        rendered = json.dumps(
            {
                "doc": document["item_id"],
                "section": section_id,
                "title": node.title,
                "sections": inside or _headings_only(inside),
            },
            ensure_ascii=False,
        )
        if len(rendered) <= budget:
            return rendered
        return json.dumps(
            {
                "doc": document["item_id"],
                "section": section_id,
                "title": node.title,
                "sections": _trim_to_fit(_headings_only(inside), budget),
            },
            ensure_ascii=False,
        )

    sections = root.outline().get("sections", [])
    rendered = json.dumps(_entry(document, sections), ensure_ascii=False)
    if len(rendered) <= budget:
        return rendered
    # Headings-only was treated as small enough by definition, and it is not.
    # Measured: open_document on a 3,604-section book returned 138,584
    # characters — about 34,600 tokens — straight into the next prompt, because
    # the fallback returned without re-checking. The query never finished.
    #
    # Checked against the FINAL rendered string, not against the section list.
    # Trimming the list alone left the wrapper unaccounted for and still came
    # back 574 characters over. A budget is about what is sent.
    headings = _headings_only(sections)
    for _ in range(4):
        rendered = json.dumps(_entry(document, _trim_to_fit(headings, budget)), ensure_ascii=False)
        if len(rendered) <= budget:
            return rendered
        budget = int(budget * 0.85)
    return rendered


def _trim_to_fit(sections: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """Cut a heading list to the budget, saying how much was cut.

    The last line of defence, and it has to exist: "headings only" is a big
    reduction on a report and no reduction at all on a book, where the headings
    ARE the size. A budget that is checked before one fallback and not after
    the next is not a budget.

    Reported rather than silent, so the agent knows it is looking at part of a
    list and can drill in instead of concluding the rest is not there.
    """
    if not sections:
        return sections
    # Measured, not estimated. Estimating from the first entry's length assumes
    # every heading is the same size, and on a real book they are not — that
    # assumption overshot a 24,000 budget by 1,203 characters. Entries are added
    # until the rendered JSON would exceed the budget, so the result fits by
    # construction rather than by arithmetic that is nearly right.
    room = budget - 200  # headroom for the wrapper this list is nested in
    kept: list[dict[str, Any]] = []
    used = 2  # the enclosing [] of the rendered list
    for entry in sections:
        size = len(json.dumps(entry, ensure_ascii=False)) + 1
        if used + size > room:
            break
        kept.append(entry)
        used += size
    if len(kept) == len(sections):
        return sections
    if not kept:
        kept = [sections[0]]
    kept.append({"headings_not_shown": len(sections) - len(kept)})
    return kept


async def navigate(
    session: AsyncSession,
    scope: Scope,
    question: str,
    on_step: Callable[[Step], None] | None = None,
    on_token: Callable[[str], None] | None = None,
    hybrid: bool = False,
    emit: EmitFn | None = None,
    only: tuple[str, ...] = (),
    vision: bool = True,
    behaviour: str = "",
) -> tuple[Outcome, Trace]:
    """Let the model read its way to an answer, and record every step.

    ``vision`` withdraws look_at_page. The walk still reads everything else, so
    a store of prose is unaffected; what changes is that a figure stops being
    readable, and a page-picture question will honestly fail rather than being
    answered from a caption. Offered because the look is the single most
    expensive step there is — measured at 65.5s of a 77.1s walk — and a caller
    who knows their documents are text should not pay for the option.

    ``behaviour`` is how the answer should be WRITTEN, not what may be said.
    It is appended to the system prompt inside a fenced section that cannot
    reach the evidence rules: style is the caller's business, grounding is not
    negotiable, and a "be confident" instruction must never become licence to
    answer from something unread. See _behaviour_note.

    ``only`` restricts the whole walk to the named documents — the explicit
    filter. Empty means "decide", which on a store larger than MAX_DOCUMENTS
    means core.routing picks the documents this question is about instead of
    the ones most recently uploaded.

    The callbacks are for watching it happen rather than waiting for it. Most of
    the time here is spent READING, not writing — opening a section, looking at
    a page — so the steps arriving live are worth more than the words arriving
    live, and both are offered.

    ``on_token``/``on_step`` are the synchronous callbacks, called from a worker
    thread because the provider client is blocking; a caller that touches an
    event loop from them must hop back to the loop itself. ``emit`` is the async
    equivalent — one event dict per step, awaited so it can be an SSE queue —
    and is what the streaming route uses.

    With ``hybrid`` the same agent also gets a hybrid_search tool — it reasons
    over each document's table of contents AND can fall back to embedding and
    keyword search to locate a specific figure or identifier, then read the
    section it lands in. This is the "agentic" retrieval mode: one loop, both
    ways of finding evidence, the agent choosing per question. The outcome is
    identical whether or not anyone was watching, which is why the unwatched
    path still makes the identical blocking call the tests pin.
    """
    started = time.perf_counter()
    outcome = Outcome()
    trace = Trace(
        trace_id=new_trace_id(),
        query=question,
        config={
            "retrieval": "agentic" if hybrid else "vectorless",
            "max_rounds": MAX_ROUNDS,
        },
        filters={"workspace_id": scope.workspace_id, "collection_id": scope.collection_id},
    )

    def record(step: Step) -> None:
        """Keep a step and, if anyone is watching, hand it over at once."""
        outcome.steps.append(step)
        if on_step is not None:
            on_step(step)

    # WHICH documents, before HOW MANY. The cap has always been MAX_DOCUMENTS;
    # what changed is that on a store bigger than the cap the forty shown are
    # now the forty this question is about, rather than the forty uploaded most
    # recently. On a store that fits, routing does not run and this is the same
    # query it always was.
    routing = await route.choose(session, scope, question, only=only, limit=MAX_DOCUMENTS)
    if routing.item_ids:
        documents = await _documents(
            session, scope, limit=MAX_DOCUMENTS, item_ids=routing.item_ids
        )
    else:
        documents = await _documents(session, scope, limit=MAX_DOCUMENTS)
    # Truncation is measured by comparing what was LOADED against what exists,
    # not by arithmetic on the cap.
    #
    # It used to fetch one past the cap and slice back with documents[:cap],
    # which is correct for any positive cap and catastrophic for a cap of 0 —
    # and 0 is now the default, meaning "no count limit". documents[:0] is the
    # empty list, so every catalogue walk was handed an empty store and reported
    # that the collection did not answer the question. Fast, confident, and
    # wrong about a book that was sitting right there. Comparing counts says the
    # same thing without depending on the cap being a positive number.
    catalogue_truncated = len(documents) < routing.available
    trace.config["routing"] = routing.as_dict()
    if routing.how != "everything":
        # The label has to match what actually happened. "ranked" excludes
        # nothing, so reporting it as "filtered 3 of 3 documents" described a
        # narrowing that never took place — and a trail that misdescribes the
        # retrieval is worse than a silent one, because it is believed.
        action = {"routed": "narrowed", "named": "filtered", "ranked": "ranked"}[routing.how]
        detail = (
            f"{len(documents)} document{'s' if len(documents) != 1 else ''} by relevance"
            if routing.how == "ranked"
            else f"{len(documents)} of {routing.available} documents"
            + (" — named in the request" if routing.how == "named" else "")
        )
        record(Step(0, action, detail))
    if not documents:
        trace.timings_ms["total"] = int((time.perf_counter() - started) * 1000)
        return outcome, trace

    # Cached on the document's content, so re-parsing only happens when the
    # document actually changed. Measured on this collection: 8.2 seconds of
    # every single question went on rebuilding trees the previous question had
    # already built.
    trees = {
        doc["item_id"]: tree.build_cached(doc["item_id"], doc["body"], doc["title"])
        for doc in documents
    }
    by_id = {doc["item_id"]: doc for doc in documents}

    # Enrich the catalogue with index summaries when they exist. Each document
    # gets a ``_card`` blurb and ``_section_summaries`` list that _catalogue
    # injects, so the agent navigates by description rather than raw headings.
    # The text lives in Postgres; graph.chunk_summaries returns only ids,
    # headings and coverage counts — the text is fetched lazily below.
    for doc in documents:
        try:
            sums = await graph.chunk_summaries(scope, doc["item_id"])
        except Exception:  # noqa: BLE001 — summaries are optional
            sums = []
        card_text = None
        section_summaries: list[dict[str, Any]] = []
        for s in sums:
            ntype = s.get("node_type") or s.get("kind", "")
            if ntype == "card":
                # Fetch the card body from Postgres.
                row = (
                    await session.execute(
                        sql(
                            "SELECT text FROM kb_chunks WHERE chunk_id = :cid AND workspace_id = :w"
                        ),
                        {"cid": s["chunk_id"], "w": scope.workspace_id},
                    )
                ).first()
                if row:
                    card_text = row.text
            elif ntype == "section_summary":
                row = (
                    await session.execute(
                        sql(
                            "SELECT text FROM kb_chunks WHERE chunk_id = :cid AND workspace_id = :w"
                        ),
                        {"cid": s["chunk_id"], "w": scope.workspace_id},
                    )
                ).first()
                if row:
                    section_summaries.append({"heading": s.get("heading", ""), "body": row.text})
        if card_text:
            doc["_card"] = card_text
        if section_summaries:
            doc["_section_summaries"] = section_summaries

    outcome.documents_considered = len(documents)
    outcome.sections_available = sum(tree.count(t) for t in trees.values())

    # Said before the first model call, not after it. Choosing what to read is
    # one round trip with every document's contents in the prompt, and on a
    # slow provider that was 13.5 seconds of a spinner with nothing behind it —
    # a wait indistinguishable from a hang. This costs nothing and is true the
    # moment it is printed.
    #
    # Both channels carry it: `record` puts it in outcome.steps for the trace
    # and the on_step sink, `emit` puts it on the wire for a watching browser.
    # They are the same fact told to two different audiences, and dropping
    # either one leaves one of them staring at nothing.
    record(
        Step(
            0,
            "opened",
            f"the index of {len(documents)} document{'s' if len(documents) != 1 else ''}, "
            f"{outcome.sections_available} sections",
        )
    )
    if emit is not None:
        await emit(
            {
                "type": "start",
                "mode": "agentic" if hybrid else "vectorless",
                "documents": outcome.documents_considered,
                "sections": outcome.sections_available,
            }
        )

    from packages.core.llm import astream_chat_with_tools, chat_with_tools

    # Only offer the graph hop where there is a graph to hop. Asked once, per
    # question, because the answer is a property of the collection rather than
    # of the round.
    can_hop = bool(hybrid) and await graph.has_near_edges(scope)
    _system = _SYSTEM + _HYBRID_NOTE + (_NEIGHBOURS_NOTE if can_hop else "") if hybrid else _SYSTEM
    # Last, so the rules above are what it is qualifying rather than replacing.
    _system += _behaviour_note(behaviour)

    catalogue, abbreviated = _catalogue(documents, trees)
    # Reaching a chapter through a shortened catalogue costs two rounds the
    # flat one never spent: open the document, open the part. Measured
    # without this, the agent used them on navigation and ran out before it
    # could read anything — five books opened, no answer. The extra rounds
    # are the price of the smaller prompt, and only charged when it applies.
    max_rounds = MAX_ROUNDS + (2 if abbreviated else 0)

    # Run inline, on the request's own session, rather than as a task started
    # earlier on a session of its own.
    #
    # The parallel version saved about a second and cost the hint entirely. Run
    # as a task and awaited inside the caller's open session scope, the second
    # session it opened could not make progress: 394ms of work timed out at 25
    # seconds, every time, and the failure was swallowed into an empty string.
    # So the agent never got section ids, fell back to open_document, and met a
    # 138,000-character outline. Two separate bugs traced back to this
    # optimisation; it is not worth one second.
    hint = (
        await _where_the_words_are(
            session, scope, question, trees, prefer=tuple(routing.order)
        )
        if abbreviated
        else ""
    )
    # Whether the index handed over section ids. When it did, open_document has
    # nothing left to tell the agent, and the tool is withheld for the opening
    # rounds — see where `tools` is built.
    hinted_sections = "read_section(doc=" in hint
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system},
        {
            "role": "user",
            "content": (
                f"DOCUMENTS:\n{catalogue}\n\n"
                + (
                    (
                        "This list is SHORTENED: it shows each document's "
                        "top-level headings only. The sections you need are "
                        "already named below — read those.\n\n"
                        if hinted_sections
                        else "This list is SHORTENED: it shows each document's "
                        "top-level headings only, without the sections inside "
                        "them or their opening lines. Call open_document on the "
                        "one that looks right to see its contents in full, then "
                        "read from there.\n\n"
                    )
                    if abbreviated
                    else ""
                )
                + hint
                + f"{_where_named_things_live(question, documents, trees)}"
                + _why_these_documents(routing, documents)
                + f"QUESTION: {question}"
            ),
        },
    ]

    # Numbered in the order they are read, so a citation can only ever point at
    # something that was actually opened.
    read: list[tuple[Passage, str]] = []
    seen: set[tuple[str, str]] = set()
    # How many pages each read document has, learned lazily: finding out costs
    # a fetch of the original, and doing it for forty documents to answer a
    # question that touches one is forty fetches wasted.
    page_counts: dict[str, int] = {}
    looks = 0
    # Set once a section has been read out of a document that IS a picture — a
    # photograph, a screenshot, a scan. Its "text" is a description somebody
    # wrote by looking at it, which is lossy by construction: asked which
    # elements are liquid, the store had a transcription of the periodic table
    # that listed every element and never said which ones were coloured as
    # liquids, and answered that it could not tell.
    #
    # A question about such a document is a question about a picture whether or
    # not the person happened to use the word. They should not have to know
    # that their file has no text in it.
    read_a_picture = False
    # Looking at the same page twice costs a second vision call to learn
    # exactly what the first one said.
    looked_at: set[tuple[str, int]] = set()
    # Both push-backs are once-only. Without that guard the first of them ate
    # every round: asked about a figure, the model said "not found", was sent
    # back, said it again, and the trail read `sent back` five times before the
    # loop gave up. A nudge repeated is not a nudge, it is a deadlock.
    pressed_to_look = False
    pressed_to_read = False
    # Whether hybrid_search has been used, and whether the agent has been sent
    # back once for concluding something is absent without it.
    searched = False
    pressed_to_search = False

    mark = time.perf_counter()
    for round_number in range(1, max_rounds + 1):
        outcome.rounds = round_number
        # read_section, [hybrid_search], submit_answer — search sits between
        # reading and answering because that is the order the agent uses them
        # in. Looking is offered only once reading has happened and only for a
        # document that actually has pages. Before that the tool does not exist
        # as far as the model is concerned, which is a stronger guarantee than
        # telling it not to.
        # Built by appending rather than by rebuilding from _TOOLS each time:
        # the gates are independent, and an agentic question against a shortened
        # catalogue needs hybrid_search, neighbors AND open_document at once.
        # neighbors only where :NEAR edges exist. Offering a tool that can only
        # ever return nothing costs a full round to discover that, and on a
        # store without consolidation running it can NEVER return anything.
        if hybrid:
            tools = [_TOOLS[0], _HYBRID_TOOL, *([_NEIGHBOURS_TOOL] if can_hop else []), _TOOLS[1]]
        else:
            tools = list(_TOOLS)
        # open_document is offered when the catalogue was shortened AND the
        # index has not already handed over section ids.
        #
        # When the hint fired, everything open_document would return is already
        # in the prompt — and the round it costs is not free: on a 3,604-section
        # book that outline is the largest single thing in the conversation.
        # Measured, the agent opened the document anyway and the question never
        # finished. Withdrawing the tool is the only reliable way to stop it;
        # saying "you do not need this" in the prompt was not enough.
        #
        # It comes BACK once reading has happened and has not settled the
        # question, so a wrong hint cannot trap the agent with no way to explore.
        if abbreviated and (not hinted_sections or read):
            tools = [*tools, _OPEN_TOOL]
        if vision and looks < MAX_LOOKS and any(page_counts.values()):
            # Reading first is NOT the waste it looks like. Offering the page
            # tool from the opening round — on the reasoning that a picture's
            # text is only a description anyway — made the agent look before it
            # knew which document it needed: it looked at the wrong one, spent
            # a look on NOT_ON_THIS_PAGE, then answered from a third document
            # entirely. Asked which elements are liquid, it described a chart
            # of support tickets. Two of twenty answers went that way.
            #
            # The read is what establishes WHERE. It costs a round and it earns
            # it.
            tools = [*tools, _LOOK_TOOL]
        try:
            if emit is not None:
                # Same turn, reported live: the model's reasoning is forwarded
                # token by token as it arrives, and the reassembled tool calls
                # come back in exactly the shape the blocking call returns.
                reply = {"content": "", "tool_calls": []}
                async for event in astream_chat_with_tools(
                    messages, tools, temperature=0.0
                ):
                    if event["type"] == "text":
                        await emit(
                            {
                                "type": "thinking",
                                "delta": event["delta"],
                                "round": round_number,
                            }
                        )
                    elif event["type"] == "error":
                        raise RuntimeError(event["error"])
                    elif event["type"] == "done":
                        reply = {
                            "content": event["content"],
                            "tool_calls": event["tool_calls"],
                        }
            elif on_token is not None:
                reply = await asyncio.to_thread(_stream_round, messages, tools, on_token)
            else:
                reply = await asyncio.to_thread(
                    chat_with_tools, messages, tools, temperature=0.0
                )
        except Exception as exc:
            outcome.degraded = f"navigation unavailable: {type(exc).__name__}"
            trace.degraded = outcome.degraded
            if emit is not None:
                await emit({"type": "degraded", "reason": outcome.degraded})
            break

        calls = reply.get("tool_calls") or []
        if not calls and not read and not pressed_to_read and round_number < max_rounds:
            # It answered without opening anything — and what it had to go on
            # was the CATALOGUE, which holds each section's title and its first
            # line. Asked for two figures out of a table, it read the openings,
            # saw no numbers, and reported the store did not contain them. The
            # store did contain them.
            #
            # Accepting that is how a store gets a confident "not found" over
            # material it never looked at, so it is sent back once. Only once,
            # and only while it has read nothing: a model that has read and
            # still says no is answering, not skipping.
            messages.append({"role": "assistant", "content": reply.get("content") or ""})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "You have not read any section yet. The list you were given "
                        "shows only each section's title and opening line — figures, "
                        "tables and detail are not in it. Call read_section on the "
                        "most likely section before deciding anything is missing."
                    ),
                }
            )
            pressed_to_read = True
            record(Step(round_number, "sent back", "answered without reading"))
            continue

        if (
            not calls
            and read
            and looks == 0
            and not pressed_to_look
            and any(page_counts.values())
            and (_about_a_picture(question) or read_a_picture)
            and round_number < max_rounds
        ):
            # The same press as below, on the path that was skipping it.
            #
            # The look was only ever demanded inside submit_answer, and this
            # model mostly does not call submit_answer — it writes the answer as
            # prose. So asked which quarter shipped most in a bar chart, it read
            # the page, found the axis labels and the quarter names, and wrote a
            # paragraph about the axis without ever looking at the bars. Four
            # questions in a row went that way, every one of them marked
            # grounded, because a genuinely-read section was genuinely cited.
            #
            # One gate on two paths, then. A question about a picture cannot be
            # answered from the text layer whichever way the answer arrives.
            messages.append({"role": "assistant", "content": reply.get("content") or ""})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "You asked about a figure, and a PDF's text layer holds its "
                        "caption but NOT what it shows — bar heights, shapes, labels "
                        "inside the drawing. Call look_at_page on the page holding it "
                        "and answer from what you see there."
                    ),
                }
            )
            pressed_to_look = True
            record(
                Step(round_number, "sent back", "answered about a picture from text alone")
            )
            continue

        if not calls:
            # It answered in prose without calling submit_answer. Taken as the
            # answer rather than discarded — refusing content the model already
            # produced would turn a formatting slip into an empty result, which
            # is the failure this whole module exists to remove.
            if reply.get("content"):
                drafted = _strip_answer_label(
                    _strip_pseudo_call(reply["content"].strip())
                )
                if _should_search_first(
                    absent=_reads_as_absent(drafted),
                    hybrid=hybrid,
                    searched=searched,
                    pressed=pressed_to_search,
                    rounds_left=round_number < max_rounds,
                ):
                    pressed_to_search = True
                    messages.append({"role": "user", "content": _SEARCH_FIRST})
                    record(Step(round_number, "sent back", "concluded absent without searching"))
                    continue
                outcome.answer = drafted
                outcome.found = bool(read)
                record(Step(round_number, "answered", "without submit_answer"))
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

        # Which document the first round of reading is allowed to touch.
        #
        # read_section takes ONE doc per call, so a model that wants three
        # documents issues three calls in one round — and that is what it does.
        # Measured on a six-document store, asked "what are the two surfaces of
        # the user experience?": five sections arrived within 0.2s of each
        # other, from the vision document, a casino SOW and the document that
        # actually answered. Two of the three contributed nothing and were
        # still cited. The question carries no distinctive word, so every
        # document looked equally plausible from its title alone.
        #
        # The ranking was already in the prompt, and stated firmly ("Start with
        # the first one. Do NOT read from a document far down this list to
        # 'check' it"). Instruction was not enough, so the first reading round
        # is held to one document — the best-ranked one it asked for.
        #
        # Deliberately only the FIRST round, and deliberately not a filter:
        # nothing is hidden, every document stays in the catalogue, and the
        # very next round may read any of them. All this buys is that the agent
        # reads ONE document before concluding it needs another — which is what
        # the ranking was telling it to do anyway.
        if not read:
            wanted = [
                str(json.loads(c["arguments"] or "{}").get("doc") or "")
                for c in calls
                if c["name"] == "read_section"
            ]
            distinct = [d for d in dict.fromkeys(wanted) if d]
            if len(distinct) > 1:
                by_rank = {doc: n for n, doc in enumerate(routing.order)}
                first_document = min(distinct, key=lambda d: by_rank.get(d, len(by_rank)))
                deferred = [d for d in distinct if d != first_document]
                record(
                    Step(
                        round_number,
                        "held back",
                        f"{len(deferred)} other document(s) until one is read",
                    )
                )
            else:
                first_document = ""
                deferred = []
        else:
            first_document = ""
            deferred = []

        finished = False
        for call in calls:
            try:
                args = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}

            # The decision itself, before it is carried out. submit_answer is
            # reported as the answer instead, below, so it is not doubled here.
            if emit is not None and call["name"] != "submit_answer":
                await emit(
                    {
                        "type": "tool_call",
                        "tool": call["name"],
                        "args": args,
                        "round": round_number,
                    }
                )

            if call["name"] == "open_document":
                doc_id = str(args.get("doc") or "")
                if doc_id not in by_id:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": "No document has that id. Use one from the list.",
                        }
                    )
                    record(Step(round_number, "missed", f"document {doc_id[:12]}"))
                    if emit is not None:
                        await emit(
                            {
                                "type": "tool_result",
                                "tool": "open_document",
                                "ok": False,
                                "detail": "no document with that id",
                            }
                        )
                    continue
                section_id = str(args.get("section") or "")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": _outline_of(
                            by_id[doc_id], trees[doc_id], CATALOGUE_BUDGET, section_id
                        ),
                    }
                )
                record(
                    Step(
                        round_number,
                        "opened",
                        f"{by_id[doc_id]['title'][:34]}"
                        + (
                            f" > {section_id}"
                            if section_id
                            else f" — {tree.count(trees[doc_id])} sections"
                        ),
                    )
                )
                if emit is not None:
                    await emit(
                        {
                            "type": "tool_result",
                            "tool": "open_document",
                            "ok": True,
                            "title": by_id[doc_id]["title"],
                            "heading": section_id,
                            "count": tree.count(trees[doc_id]),
                        }
                    )
                continue

            if call["name"] == "submit_answer":
                said_found = bool(args.get("found"))

                if (
                    not said_found
                    and not read
                    and not pressed_to_read
                    and round_number < max_rounds
                ):
                    # "Not here", decided without opening anything. Identical in
                    # substance to answering in prose without reading, so it
                    # gets the same push-back — a conclusion about a document
                    # nobody looked in is a guess about a table of contents.
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": (
                                "You have not read any section. The list you were given "
                                "holds only titles and opening lines. Read the most "
                                "likely section before concluding anything is missing."
                            ),
                        }
                    )
                    pressed_to_read = True
                    record(
                        Step(round_number, "sent back", "said not found without reading")
                    )
                    continue

                if (
                    read
                    and looks == 0
                    and not pressed_to_look
                    and any(page_counts.values())
                    and round_number < max_rounds
                    and (not said_found or _about_a_picture(question) or read_a_picture)
                ):
                    # Two ways an answer about a picture goes wrong, and only
                    # one of them announces itself.
                    #
                    # The loud one: it reads the text, finds nothing, and says
                    # the document does not cover it — while the answer sits in
                    # the diagram on the page it just read.
                    #
                    # The quiet one, which is worse: asked what sits above the
                    # decoder in Figure 1, it read the prose about sub-layers
                    # and answered confidently from THAT — naming a layer-norm
                    # the figure does not have — and the answer was marked
                    # grounded, because it did cite the section it read. A
                    # wrong answer with a citation behind it is the single most
                    # expensive thing this store can produce.
                    #
                    # So the question itself is the trigger for the quiet case.
                    # When someone asks about a figure by name, the text layer
                    # is KNOWN not to contain the answer — captions survive,
                    # contents do not — and answering from prose is a guess
                    # dressed as a reading. Pressed once, either way.
                    pressed_to_look = True
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": (
                                "Not yet. You asked about a figure or diagram, and a "
                                "PDF's text layer carries captions but NOT what the "
                                "picture shows — so neither an answer nor a 'not "
                                "found' can come from the prose alone. Call "
                                "look_at_page on the page holding it, then answer. "
                                "If you have looked and it is genuinely not there, "
                                "submit_answer again."
                                if said_found
                                else "Before you conclude that: the text of a PDF does "
                                "not contain what its figures and diagrams SHOW. If the "
                                "answer could be in a picture on a page you have read, "
                                "call look_at_page on that page. If you have looked and "
                                "it is genuinely not there, submit_answer again."
                            ),
                        }
                    )
                    record(
                        Step(
                            round_number,
                            "sent back",
                            "answered about a picture from text alone"
                            if said_found
                            else "not found without looking at a page",
                        )
                    )
                    continue

                drafted = str(args.get("answer") or "").strip()
                if _should_search_first(
                    absent=(not said_found) or _reads_as_absent(drafted),
                    hybrid=hybrid,
                    searched=searched,
                    pressed=pressed_to_search,
                    rounds_left=round_number < max_rounds,
                ):
                    pressed_to_search = True
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": _SEARCH_FIRST,
                        }
                    )
                    record(Step(round_number, "sent back", "concluded absent without searching"))
                    continue

                outcome.answer = drafted
                outcome.found = said_found and bool(read)
                record(
                    Step(round_number, "answered", "found" if outcome.found else "not found")
                )
                if emit is not None:
                    await emit(
                        {"type": "answer", "text": outcome.answer, "found": outcome.found}
                    )
                finished = True
                break

            if call["name"] == "look_at_page":
                if looks >= MAX_LOOKS:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": "No more pages may be looked at. Answer now.",
                        }
                    )
                    continue

                doc_id = str(args.get("doc") or "")
                if doc_id not in page_counts and len(page_counts) == 1:
                    doc_id = next(iter(page_counts))

                # A picture can be looked at without being read first, so its
                # page count may not have been learned yet. Found now rather
                # than at the start: counting means fetching the original, and
                # doing that for every document in the store to answer a
                # question about one of them is forty fetches wasted.
                if doc_id not in page_counts and doc_id in by_id:
                    from packages.core import pages as page_store

                    page_counts[doc_id] = (
                        await page_store.count(
                            scope.workspace_id, doc_id, by_id[doc_id]["locator"] or ""
                        )
                        if page_store.available()
                        else 0
                    )
                total = page_counts.get(doc_id, 0)
                try:
                    page_number = int(args.get("page") or 0)
                except (TypeError, ValueError):
                    page_number = 0

                if not 1 <= page_number <= total:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": (
                                f"That document has pages 1–{total}."
                                if total
                                else "That document has no pages to look at."
                            ),
                        }
                    )
                    record(Step(round_number, "missed", f"page {page_number}"))
                    continue

                if (doc_id, page_number) in looked_at:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": (
                                "You have already looked at that page. Look at a "
                                "different one, or answer from what you have."
                            ),
                        }
                    )
                    continue
                looked_at.add((doc_id, page_number))

                looks += 1
                wanted = str(args.get("looking_for") or question)
                seeing = await _read_page(scope.workspace_id, doc_id, page_number, wanted)
                if seeing is None:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": (
                                "That page could not be read as a picture. Work "
                                "from the text you have."
                            ),
                        }
                    )
                    record(
                        Step(round_number, "missed", f"page {page_number} (unreadable)")
                    )
                    continue

                if seeing.startswith(_NOT_ON_PAGE):
                    # A refusal on a document that IS this page is not evidence
                    # about the page — it is the steer failing. The agent read
                    # the document's description and chose this page on purpose;
                    # there is no other page it could have meant. Asked which
                    # elements are liquid, the periodic table came back
                    # NOT_ON_THIS_PAGE roughly one run in three, and the same
                    # page read without a steer answered every time.
                    #
                    # So the steer is dropped and the page is read plainly, once.
                    # Same lesson as the focused-prompt experiment above: telling
                    # a vision model what to care about is what makes it stop
                    # looking at the rest.
                    if _only_page_of_a_picture(by_id.get(doc_id), page_counts.get(doc_id, 0)):
                        again = await _read_page(
                            scope.workspace_id, doc_id, page_number, _UNSTEERED
                        )
                        if again is not None and not again.startswith(_NOT_ON_PAGE):
                            seeing = again

                if seeing.startswith(_NOT_ON_PAGE):
                    title = by_id[doc_id]["title"] if doc_id in by_id else doc_id
                    # "Answer from the text" is what this used to say, and on a
                    # picture that sentence is a trap: the document's only text
                    # IS a description of the page that just came back empty.
                    # Asked which elements are liquid, the agent looked at a
                    # support-tickets chart, was told the page had nothing to do
                    # with the question, then read that same chart's description
                    # and wrote a paragraph about support tickets. A document
                    # that has just disclaimed the question must not become the
                    # source of the answer to it.
                    ruled_out = (
                        doc_id in by_id
                        and _is_a_picture(by_id[doc_id])
                        and page_counts.get(doc_id, 0) <= 1
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": (
                                f"Page {page_number} of {title} has nothing to do "
                                "with the question."
                                + (
                                    " That document is a picture and that was its "
                                    "only page, so it cannot answer this — its "
                                    "stored text is only a description of the page "
                                    "you just saw. Do not answer from it. Choose a "
                                    "different document."
                                    if ruled_out
                                    else " Try another page, or another document."
                                )
                            ),
                        }
                    )
                    # Recorded, not hidden: a look that came back empty is the
                    # reader's evidence that the page was checked and did not
                    # hold the answer. Named, too — "page 1 — not there" over a
                    # store where every picture has a page 1 says nothing.
                    record(
                        Step(
                            round_number,
                            "looked",
                            f"{title[:40]} page {page_number} — not there",
                        )
                    )
                    continue

                # The coordinates come off the prose here rather than inside
                # _read_page, so a reading is still just text to everything
                # that does not draw.
                seeing, regions = _regions_in(seeing)
                marker = len(read) + 1
                read.append(
                    (
                        Passage(
                            chunk_id=f"{doc_id}:page:{page_number}",
                            ordinal=marker - 1,
                            heading=f"Page {page_number}",
                            text=seeing,
                            score=round(1.0 - (marker - 1) * 0.05, 4),
                            page=page_number,
                            regions=regions,
                        ),
                        doc_id,
                    )
                )
                record(
                    Step(round_number, "looked", f"page {page_number} → [{marker}]")
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": (
                            f"[{marker}] {by_id[doc_id]['title']} > page {page_number} "
                            f"(read from the page image)\n\n{seeing}\n\n"
                            f"(Cite this as [{marker}].)"
                        ),
                    }
                )
                continue

            if call["name"] == "hybrid_search" and hybrid:
                # Recorded even when the search returns nothing: the guard below
                # asks whether the agent LOOKED, and a search that found nothing
                # is still having looked.
                searched = True
                found_query = str(args.get("query") or "").strip()
                if not found_query:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": "Provide a non-empty query.",
                        }
                    )
                    continue
                # Real hybrid retrieval — embeddings + keyword, fused. Its own
                # trace is discarded; the navigator's is the record. The best
                # passages across the returned documents become citable reads,
                # numbered in the same sequence as sections, so downstream sees
                # one uniform shape however the evidence was found.
                # Scoped to the documents in front of the agent. Without this
                # the search tool reaches the WHOLE store and hands back a
                # passage from a document the router (or an explicit filter)
                # already excluded — the agent then cites it, and a question
                # scoped to one document is answered from another.
                hybrid_hits, _ = await search_traced(
                    session,
                    scope,
                    found_query,
                    # No expansion here. The agent has already CHOSEN these
                    # words — that is what the tool is for — so asking a model
                    # what else to look for is guessing at a deliberate query.
                    # Measured: agentic went from 5s to 70s once every search
                    # the agent made spent its own expansion call, and the
                    # cache cannot help because each of its queries is new.
                    RetrievalConfig(limit=5, item_ids=tuple(by_id), expand_query=False),
                )
                pairs = [(p, h) for h in hybrid_hits for p in h.passages]
                pairs.sort(key=lambda pr: pr[0].score, reverse=True)
                surfaced: list[str] = []
                surfaced_headings: list[str] = []
                for passage, hit in pairs[:MAX_HYBRID_HITS]:
                    key = (hit.item_id, passage.chunk_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    # A hybrid hit may point at a document outside the top-N
                    # catalogue, so register its metadata for the final rollup.
                    by_id.setdefault(
                        hit.item_id,
                        {
                            "item_id": hit.item_id,
                            "title": hit.title,
                            "body": "",
                            "source": hit.source.source,
                            "locator": hit.source.locator,
                            "url": hit.source.url,
                        },
                    )
                    marker = len(read) + 1
                    body = passage.text[:MAX_SECTION_CHARS]
                    read.append(
                        (
                            Passage(
                                chunk_id=passage.chunk_id,
                                ordinal=marker - 1,
                                heading=passage.heading,
                                text=body,
                                score=passage.score,
                            ),
                            hit.item_id,
                        )
                    )
                    surfaced.append(
                        f"[{marker}] {hit.title} > {passage.heading}\n\n{body}\n\n"
                        f"(Cite this as [{marker}]. To read passages near this one, "
                        f"call neighbors with chunk_id {passage.chunk_id!r}.)"
                    )
                    surfaced_headings.append(passage.heading or hit.title)
                    record(
                        Step(round_number, "searched", f"{passage.heading[:50]} → [{marker}]")
                    )
                if surfaced:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": "\n\n".join(surfaced),
                        }
                    )
                    if emit is not None:
                        await emit(
                            {
                                "type": "tool_result",
                                "tool": "hybrid_search",
                                "ok": True,
                                "count": len(surfaced),
                                "headings": surfaced_headings,
                            }
                        )
                    if len(read) >= MAX_READS:
                        messages.append(
                            {
                                "role": "user",
                                "content": "That is enough reading. Answer now with submit_answer.",
                            }
                        )
                else:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": (
                                f"hybrid_search for {found_query!r} surfaced nothing new. "
                                "Try different wording, or answer from what you have."
                            ),
                        }
                    )
                    record(
                        Step(round_number, "searched", f"{found_query[:50]} → nothing")
                    )
                    if emit is not None:
                        await emit(
                            {
                                "type": "tool_result",
                                "tool": "hybrid_search",
                                "ok": False,
                                "count": 0,
                            }
                        )
                continue

            if call["name"] == "neighbors" and hybrid:
                origin = str(args.get("chunk_id") or "").strip()
                if not origin:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": "Provide the chunk_id of a passage a hybrid_search returned.",
                        }
                    )
                    continue
                # The stored :NEAR edges — the graph as consolidation organised
                # it. The neighbour manifest carries ids and headings but not
                # text, so the bodies are hydrated from Postgres and turned into
                # citable reads, numbered in the same sequence as everything else.
                # `graph` is imported at module level (the catalogue enrichment
                # above needs it). A second import HERE would rebind it as a
                # local for the whole of navigate, and the enrichment — which
                # runs hundreds of lines earlier — would raise UnboundLocalError
                # before this line was ever reached.
                from packages.core import chunks as chunk_store

                neighbours = await graph.chunk_neighbours(scope, origin, limit=MAX_HYBRID_HITS)
                hydrated = await chunk_store.by_ids(
                    session, scope, [n["chunk_id"] for n in neighbours]
                )
                surfaced = []
                surfaced_headings = []
                for n in neighbours:
                    stored = hydrated.get(n["chunk_id"])
                    if stored is None:
                        continue
                    key = (n["item_id"], n["chunk_id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    by_id.setdefault(
                        n["item_id"],
                        {
                            "item_id": n["item_id"],
                            "title": n["title"] or n["item_id"],
                            "body": "",
                            "source": "",
                            "locator": "",
                            "url": None,
                        },
                    )
                    marker = len(read) + 1
                    heading = n["heading"] or stored.heading
                    body = stored.text[:MAX_SECTION_CHARS]
                    read.append(
                        (
                            Passage(
                                chunk_id=n["chunk_id"],
                                ordinal=marker - 1,
                                heading=heading,
                                text=body,
                                score=round(float(n["similarity"] or 0.0), 4),
                            ),
                            n["item_id"],
                        )
                    )
                    surfaced.append(
                        f"[{marker}] {by_id[n['item_id']]['title']} > {heading}\n\n{body}\n\n"
                        f"(Cite this as [{marker}]. chunk_id {n['chunk_id']!r} — call "
                        f"neighbors on it to keep exploring.)"
                    )
                    surfaced_headings.append(heading or n["title"])
                    record(Step(round_number, "hopped", f"{heading[:50]} → [{marker}]"))
                if surfaced:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": "\n\n".join(surfaced),
                        }
                    )
                    if emit is not None:
                        await emit(
                            {
                                "type": "tool_result",
                                "tool": "neighbors",
                                "ok": True,
                                "count": len(surfaced),
                                "headings": surfaced_headings,
                            }
                        )
                    if len(read) >= MAX_READS:
                        messages.append(
                            {
                                "role": "user",
                                "content": "That is enough reading. Answer now with submit_answer.",
                            }
                        )
                else:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": (
                                f"Nothing live neighbours {origin!r}, or its neighbours were "
                                "already read. Answer from what you have, or search anew."
                            ),
                        }
                    )
                    record(Step(round_number, "hopped", f"{origin[:40]} → nothing"))
                    if emit is not None:
                        await emit(
                            {"type": "tool_result", "tool": "neighbors", "ok": False, "count": 0}
                        )
                continue

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

            # Deferred, not refused, and told plainly why — a tool result the
            # model cannot act on is how a walk stalls. It keeps the section
            # ids, so taking this up costs one round and no re-navigation.
            if first_document and doc_id in deferred:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": (
                            "Not yet — read one document before opening several. "
                            f"You asked for {len(deferred) + 1} documents at once; "
                            "the highest-ranked one is being read now and its text "
                            "follows. If it answers the question, answer. If it "
                            "does not, ask for these sections again in the next "
                            "round and they will be read."
                        ),
                    }
                )
                continue

            # One id or several. Reading three sections used to cost three
            # rounds — three model round trips — to fetch text the loop could
            # have returned in one. Measured: pages 3325, 3326 and 3327 read
            # one per round, ~15 seconds, for an answer that needed all three.
            raw_sections = args.get("section")
            if isinstance(raw_sections, list):
                node_ids = [str(s) for s in raw_sections if str(s).strip()]
            else:
                node_ids = [str(raw_sections or "")]

            # Capped, because making batching free made it shotgun. Measured
            # immediately after the batch change: asked for the three Deathly
            # Hallows, the agent requested 299 sections in one call — every
            # section from n3306 to n3604. Only MAX_READS of them could be read,
            # and the other 293 each returned a "limit reached" line, so the
            # prompt filled with refusals and the walk stopped being navigation
            # and became a scan.
            #
            # Trimmed rather than refused: the first few are the ones it thought
            # most likely, and reading those is exactly right.
            overflow = len(node_ids) - MAX_SECTIONS_PER_CALL
            node_ids = node_ids[:MAX_SECTIONS_PER_CALL]

            root = trees.get(doc_id)
            if root is None and len(trees) == 1:
                # A section id with the wrong document attached, when there is
                # only one document it could belong to. Correcting that is not
                # a guess.
                doc_id, root = next(iter(trees.items()))

            # Every section in this call answers into ONE tool message, because
            # the protocol allows exactly one reply per tool_call_id. Failures
            # are reported inside it rather than swallowed — a section the model
            # asked for and did not get has to come back as a line it can read,
            # or it will cite a number it was never given.
            parts: list[str] = []
            read_any = False
            for node_id in node_ids:
                nodes = tree.find(root, [node_id]) if root else []
                if not nodes:
                    # Said plainly so the model can try another rather than
                    # treating silence as "nothing is there".
                    parts.append(
                        f"No section {node_id!r} in that document. "
                        "Check the ids in the catalogue and try another."
                    )
                    record(Step(round_number, "missed", node_id))
                    if emit is not None:
                        await emit(
                            {
                                "type": "tool_result",
                                "tool": "read_section",
                                "ok": False,
                                "detail": f"no section {node_id!r}",
                            }
                        )
                    continue

                node = nodes[0]
                if (doc_id, node_id) in seen:
                    parts.append(
                        f"Section {node_id!r}: already read. "
                        "Read a different section, or answer."
                    )
                    if emit is not None:
                        await emit(
                            {
                                "type": "tool_result",
                                "tool": "read_section",
                                "ok": False,
                                "detail": "already read",
                            }
                        )
                    continue

                if len(read) >= MAX_READS:
                    # The budget is spent. Said here rather than silently
                    # dropping the rest of the batch, so the model knows the
                    # sections it asked for were not read.
                    parts.append(
                        f"Section {node_id!r} not read: the reading limit of "
                        f"{MAX_READS} sections is reached. Answer from what you have."
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
                            # Where this section sits in the original. It was
                            # read as text, not off a picture — but a reader
                            # checking a transcribed table wants the page
                            # whether the words were read at ingest or
                            # mid-question, and for a scan every passage came
                            # off a page.
                            #
                            # Only when that page can be RENDERED, though. A
                            # deck writes the same page markers a PDF does, so
                            # slide 3 claimed a picture nothing could produce
                            # and the console drew a broken image.
                            page=(
                                tree.page_of(by_id[doc_id]["body"] or "", node)
                                if _has_pictures(by_id[doc_id])
                                else None
                            ),
                        ),
                        doc_id,
                    )
                )
                read_any = True
                record(Step(round_number, "read", f"{node.title[:60]} → [{marker}]"))
                if emit is not None:
                    await emit(
                        {
                            "type": "tool_result",
                            "tool": "read_section",
                            "ok": True,
                            "heading": node.title,
                            "title": by_id[doc_id]["title"],
                            "marker": marker,
                        }
                    )
                parts.append(
                    f"[{marker}] {by_id[doc_id]['title']} > {node.title}\n\n{body}\n\n"
                    f"(Cite this as [{marker}].)"
                )

            if read_any and _is_a_picture(by_id[doc_id]):
                if not _already_transcribed(by_id[doc_id]):
                    read_a_picture = True

            # Now that this document has been opened, find out whether it has
            # pages to fall back on — and say so only if it does. An offer of
            # something that is not there is worse than no offer.
            offer = ""
            if read_any:
                if doc_id not in page_counts:
                    from packages.core import pages as page_store

                    page_counts[doc_id] = (
                        await page_store.count(
                            scope.workspace_id, doc_id, by_id[doc_id]["locator"] or ""
                        )
                        if page_store.available()
                        else 0
                    )
                if _already_transcribed(by_id[doc_id]) and _is_a_picture(by_id[doc_id]):
                    # Say what this text IS, rather than offering a second look
                    # at the thing it already came from.
                    #
                    # The offer below is written for a PDF, where the text layer
                    # and the page picture are genuinely different things. Given
                    # verbatim about an image it is not just useless but untrue —
                    # it implies detail is being held back — and the model took
                    # it up: 51.9s of a 63.5s walk spent looking at a diagram
                    # whose reading was already in front of it. Nothing forced
                    # that one; the offer invited it.
                    offer = (
                        "\n\n(The text above is a vision model's reading of this "
                        "whole image, made when it was indexed — it is not an "
                        "extracted text layer. Looking at the page again shows "
                        "the same picture this was read from.)"
                    )
                elif page_counts[doc_id] and looks < MAX_LOOKS:
                    offer = (
                        f"\n\n(This document has {page_counts[doc_id]} pages. If the "
                        "text above is there but unusable — a table whose columns "
                        "have collapsed, a form, a chart — look_at_page will read "
                        "the page picture instead.)"
                    )

            if overflow > 0:
                # One line, not one per dropped section.
                parts.append(
                    f"({overflow} further section ids were not read: at most "
                    f"{MAX_SECTIONS_PER_CALL} may be read per call. Ask for the "
                    "most likely ones, not a range.)"
                )

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": "\n\n---\n\n".join(parts) + offer,
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

    if not outcome.answer and read:
        # Rounds ran out mid-search: five sections opened and no conclusion
        # drawn. Returning an empty string put a blank answer on screen under a
        # heading claiming it was what the store found, which reads as a broken
        # page rather than as what it is.
        outcome.answer = (
            "I read " + str(len(read)) + " section(s) without reaching an answer. "
            "The passages below are what was opened."
        )
        record(Step(outcome.rounds, "gave up", "out of rounds"))

    # A catalogue-only answer over a collection too big to lay out saw part of
    # it. Say so — silently answering from a partial view is the failure this
    # store treats as its most dangerous. Agentic needs no such warning: its
    # hybrid_search reaches the rest, so this is scoped to the vectorless path
    # and never clobbers a real degradation.
    #
    # The wording follows what actually happened. "The most recent" was true
    # when the cut was by upload date; it is a lie once routing has ranked the
    # documents against the question, and a warning that misdescribes the
    # shortfall is worse than none — it sends people looking in the wrong place.
    if catalogue_truncated and not hybrid and outcome.degraded is None:
        outcome.degraded = (
            f"This collection has {routing.available} documents; catalogue "
            f"retrieval read {len(documents)} of them, "
            + (
                "chosen by matching the question against the index."
                if routing.routed
                else "the most recently added."
            )
            + " Ask again with agentic or hybrid retrieval to reach the whole"
            " collection."
        )

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
            # Name the document, not its id — a trace of bare hashes is unreadable.
            "title": by_id[doc_id]["title"],
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
