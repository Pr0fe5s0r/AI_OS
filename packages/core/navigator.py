from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
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
# Looking at a page costs a second model call on a bigger model. Two is enough
# to check a table and the page after it; more than that is the agent browsing.
MAX_LOOKS = 2

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

# What the page reader is asked for. A transcription, not an answer: the
# navigator does the reasoning, and a vision model that both reads AND
# concludes gives you a conclusion with no way back to what was on the page.
_LOOK_PROMPT = (
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
    return (seen or "").strip() or None


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
    session: AsyncSession,
    scope: Scope,
    question: str,
    on_step: Callable[[Step], None] | None = None,
    on_token: Callable[[str], None] | None = None,
) -> tuple[Outcome, Trace]:
    """Let the model read its way to an answer, and record every step.

    The two callbacks are for watching it happen rather than waiting for it.
    Most of the time here is spent READING, not writing — opening a section,
    looking at a page — so the steps arriving live are worth more than the
    words arriving live, and both are offered.

    ``on_token`` is called from a worker thread, because the provider client is
    blocking. A caller that touches an event loop from it must hop back to the
    loop itself; that is the caller's business and not something to hide here.
    """
    started = time.perf_counter()
    outcome = Outcome()
    trace = Trace(
        trace_id=new_trace_id(),
        query=question,
        config={"retrieval": "vectorless", "max_rounds": MAX_ROUNDS},
        filters={"workspace_id": scope.workspace_id, "collection_id": scope.collection_id},
    )

    def record(step: Step) -> None:
        """Keep a step and, if anyone is watching, hand it over at once."""
        outcome.steps.append(step)
        if on_step is not None:
            on_step(step)

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
                f"DOCUMENTS:\n{_catalogue(documents, trees)}\n\n"
                f"{_where_named_things_live(question, documents, trees)}"
                f"QUESTION: {question}"
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

    mark = time.perf_counter()
    for round_number in range(1, MAX_ROUNDS + 1):
        outcome.rounds = round_number
        # Looking is offered only once reading has happened and only for a
        # document that actually has pages. Before that the tool does not exist
        # as far as the model is concerned, which is a stronger guarantee than
        # telling it not to.
        tools = _TOOLS
        if looks < MAX_LOOKS and any(page_counts.values()):
            tools = [*_TOOLS, _LOOK_TOOL]
        try:
            if on_token is not None:
                reply = await asyncio.to_thread(_stream_round, messages, tools, on_token)
            else:
                reply = await asyncio.to_thread(
                    chat_with_tools, messages, tools, temperature=0.0
                )
        except Exception as exc:
            outcome.degraded = f"navigation unavailable: {type(exc).__name__}"
            trace.degraded = outcome.degraded
            break

        calls = reply.get("tool_calls") or []
        if not calls and not read and not pressed_to_read and round_number < MAX_ROUNDS:
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
            and round_number < MAX_ROUNDS
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
                outcome.answer = _strip_pseudo_call(reply["content"].strip())
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

        finished = False
        for call in calls:
            try:
                args = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}

            if call["name"] == "submit_answer":
                said_found = bool(args.get("found"))

                if (
                    not said_found
                    and not read
                    and not pressed_to_read
                    and round_number < MAX_ROUNDS
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
                    and round_number < MAX_ROUNDS
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

                outcome.answer = str(args.get("answer") or "").strip()
                outcome.found = said_found and bool(read)
                record(
                    Step(round_number, "answered", "found" if outcome.found else "not found")
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
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": (
                                f"Page {page_number} does not show that. Try another "
                                "page, or answer from the text."
                            ),
                        }
                    )
                    # Recorded, not hidden: a look that came back empty is the
                    # reader's evidence that the page was checked and did not
                    # hold the answer.
                    record(
                        Step(round_number, "looked", f"page {page_number} — not there")
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
                record(Step(round_number, "missed", node_id))
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
                        # Where this section sits in the original. It was read
                        # as text, not off a picture — but a reader checking a
                        # transcribed table wants the page whether the words
                        # were read at ingest or mid-question, and for a scan
                        # every passage came off a page.
                        #
                        # Only when that page can be RENDERED, though. A deck
                        # writes the same page markers a PDF does, so slide 3
                        # claimed a picture nothing could produce and the
                        # console drew a broken image.
                        page=(
                            tree.page_of(by_id[doc_id]["body"] or "", node)
                            if _has_pictures(by_id[doc_id])
                            else None
                        ),
                    ),
                    doc_id,
                )
            )
            record(Step(round_number, "read", f"{node.title[:60]} → [{marker}]"))
            if _is_a_picture(by_id[doc_id]):
                read_a_picture = True

            # Now that this document has been opened, find out whether it has
            # pages to fall back on — and say so only if it does. An offer of
            # something that is not there is worse than no offer.
            if doc_id not in page_counts:
                from packages.core import pages as page_store

                page_counts[doc_id] = (
                    await page_store.count(
                        scope.workspace_id, doc_id, by_id[doc_id]["locator"] or ""
                    )
                    if page_store.available()
                    else 0
                )
            offer = ""
            if page_counts[doc_id] and looks < MAX_LOOKS:
                offer = (
                    f"\n\n(This document has {page_counts[doc_id]} pages. If the text "
                    "above is there but unusable — a table whose columns have "
                    "collapsed, a form, a chart — look_at_page will read the page "
                    "picture instead.)"
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": (
                        f"[{marker}] {by_id[doc_id]['title']} > {node.title}\n\n{body}\n\n"
                        f"(Cite this as [{marker}].){offer}"
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
