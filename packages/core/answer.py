from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import support
from packages.core.search import DEFAULT, RetrievalConfig, Trace, search_traced
from packages.shared.schema import Hit, Passage, Scope

# A progress sink, matching the navigator's: one event dict per step, awaited so
# it can be an SSE queue. None means nobody is watching and nothing is emitted.
EmitFn = Callable[[dict[str, Any]], Awaitable[None]]

# ---------------------------------------------------------------------------
# ANSWERING — retrieval, then a written answer built only from what it found.
#
# The console used to show passages and nothing else, on the principle that
# every line on screen should be real text from a real document. That principle
# is not abandoned here, it is enforced differently: the answer is written, but
# it must cite the passages it came from, the passages stay on screen beneath
# it, and a citation that does not resolve is removed rather than shown.
#
# Three rules, all of them about refusing to invent:
#
#   1. No passages, no model call. An empty context is exactly the condition
#      under which a language model will confabulate most confidently, so the
#      question is answered as "nothing here covers that" without asking it.
#   2. The model is told to say when the passages do not answer the question,
#      and that answer is reported as ungrounded rather than dressed up.
#   3. Citations are checked against the passages that were actually supplied.
#      A reference pointing at nothing is worse than no reference, because it
#      looks checkable — the same reason the heading and the quote were made to
#      come from one passage.
#
# If the model is unreachable the passages are still returned, with the reason
# said out loud. A degraded answer that looks like a healthy one is the most
# dangerous thing this system could produce.
# ---------------------------------------------------------------------------

# How many passages are put in front of the model. Beyond roughly this many the
# relevant one gets buried and the answer drifts toward whatever came first.
MAX_PASSAGES = 8
# Long passages are trimmed rather than dropped: a truncated relevant passage
# still answers, a missing one cannot.
MAX_PASSAGE_CHARS = 1200

_SYSTEM = (
    "You answer questions using ONLY the numbered passages provided. "
    "Rules, without exception:\n"
    "1. Every claim must come from the passages. Never add knowledge of your own.\n"
    "2. Cite the passage after each claim, like [1] or [2][3].\n"
    "3. If the passages do not answer the question, reply with exactly: "
    "NOT_IN_CONTEXT followed by one sentence saying what is missing.\n"
    "4. Be direct. No preamble, no 'based on the passages', no restating the question.\n"
    "5. If the passages disagree, say so and cite both."
)

_NOT_FOUND = "NOT_IN_CONTEXT"
_CITATION = re.compile(r"\[(\d+)\]")


@dataclass(slots=True)
class Citation:
    """A passage the answer actually leaned on."""

    marker: int  # the [n] the reader sees
    chunk_id: str
    item_id: str
    title: str
    heading: str
    text: str
    score: float
    # The page it was read off, when it was read from a picture rather than
    # from text. The reader gets shown that page beside the words, which is
    # what makes a transcribed table checkable instead of merely plausible.
    page: int | None = None
    # And where on that page, so the highlight lands on the row that was read
    # rather than leaving the reader to search the page themselves.
    regions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class Answer:
    """A written answer, and everything needed to check it."""

    question: str
    text: str
    citations: list[Citation] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)
    trace_id: str = ""
    took_ms: int = 0
    # False when the store had nothing to answer from, or the model said so.
    # The distinction between "no answer" and "an answer" must survive to the
    # screen, so it is a field rather than something inferred from the prose.
    grounded: bool = True
    degraded: str | None = None
    # What the agent did to get here: which sections it opened, which ids it
    # reached for and missed, where it stopped. Carried on the answer rather
    # than left in the trace, because "why should I believe this" is answered
    # by the route taken, and a reader will not go and open a trace to find it.
    # Empty for hybrid, which has no route — it ranks and hands over.
    steps: list[dict[str, Any]] = field(default_factory=list)
    # Which retrieval produced the evidence. Travels with the answer because
    # two answers to the same question can differ entirely on this, and a
    # reader comparing them needs to know which they are looking at.
    mode: str = "agentic"

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.text,
            "mode": self.mode,
            "grounded": self.grounded,
            "degraded": self.degraded,
            "trace_id": self.trace_id,
            "took_ms": self.took_ms,
            "steps": self.steps,
            "citations": [
                {
                    "marker": c.marker,
                    "chunk_id": c.chunk_id,
                    "item_id": c.item_id,
                    "title": c.title,
                    "heading": c.heading,
                    "text": c.text,
                    "score": c.score,
                    "page": c.page,
                    "regions": c.regions,
                }
                for c in self.citations
            ],
        }


def _gather(hits: list[Hit], limit: int = MAX_PASSAGES) -> list[tuple[Passage, Hit]]:
    """The best passages across every document, best first.

    Ranked globally rather than per document: the answer wants the strongest
    evidence wherever it lives, and taking the top passage from each document
    in turn would push a document's second-best above another's best.
    """
    pairs = [(passage, hit) for hit in hits for passage in hit.passages]
    pairs.sort(key=lambda pair: pair[0].score, reverse=True)
    return pairs[:limit]


def _prompt(question: str, passages: list[tuple[Passage, Hit]]) -> str:
    blocks = []
    for index, (passage, hit) in enumerate(passages, start=1):
        where = " > ".join(part for part in (hit.title, passage.heading) if part)
        body = passage.text[:MAX_PASSAGE_CHARS]
        blocks.append(f"[{index}] {where}\n{body}")
    joined = "\n\n".join(blocks)
    return f"PASSAGES:\n\n{joined}\n\nQUESTION: {question}"


def _resolve_citations(
    text: str, passages: list[tuple[Passage, Hit]]
) -> tuple[str, list[Citation]]:
    """Keep the markers that point at a real passage; drop the rest.

    A model occasionally cites a number it was never given. Left in place that
    is a reference the reader cannot follow, so it is stripped from the prose
    and never appears in the citation list.
    """
    used: dict[int, Citation] = {}
    for match in _CITATION.finditer(text):
        marker = int(match.group(1))
        if not 1 <= marker <= len(passages):
            continue
        if marker in used:
            continue
        passage, hit = passages[marker - 1]
        used[marker] = Citation(
            marker=marker,
            chunk_id=passage.chunk_id,
            item_id=hit.item_id,
            title=hit.title,
            heading=passage.heading,
            text=passage.text,
            score=passage.score,
            page=passage.page,
            regions=passage.regions,
        )

    def keep(match: re.Match[str]) -> str:
        return match.group(0) if int(match.group(1)) in used else ""

    cleaned = _CITATION.sub(keep, text)
    # Tidy the spacing a removed marker leaves behind.
    cleaned = re.sub(r" +([.,;:])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, [used[m] for m in sorted(used)]


# How many consecutive words of the answer must appear verbatim in a passage
# before that passage is accepted as its source. Long enough that ordinary
# phrasing ("must be submitted within") cannot match by chance; short enough to
# survive the model changing a word or two.
_SHINGLE = 7


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


# A capitalised word of three letters or more: the SHAPE of a proper noun,
# which on its own proves nothing — every sentence starts with one.
_CAPITALISED = re.compile(r"\b[A-Z][A-Za-z'’-]{2,}\b")


def _used_as_a_name(text: str, word: str) -> bool:
    """Does ``word`` appear capitalised MID-SENTENCE in this text?

    This is what separates a name from a sentence opener, and the separation
    has to be made in the PASSAGE rather than in the answer: a one-word answer
    is its own sentence, so "Hedwig" and "Receipts" look identical there. In
    running prose they do not. "carried Errol to Hedwig's cage" keeps its
    capital in the middle of a clause because it is a name; "Receipts must be
    filed." only has one because of where the sentence began.

    Caught by an existing test, which is the only reason this is not simply
    capitalisation: crediting "Receipts must be" to the one passage using
    those words is the precise failure the strict rule was written to prevent.
    """
    for match in re.finditer(r"\b" + re.escape(word) + r"\b", text):
        gap = text[: match.start()]
        before = gap.rstrip()
        if not before:
            continue  # the very start of the passage
        if before[-1] in ".!?:":
            continue  # a new sentence, or the tail of a label
        # A line break is a sentence boundary too, and missing that credited
        # "Receipts must be" to the handbook: the passage reads
        # "## 1. Expenses\n\nReceipts must be submitted…", so the word after
        # the heading looked mid-sentence purely because headings carry no
        # full stop. Any heading, list item or table cell would do the same.
        if "\n" in gap[len(before) :]:
            continue
        return True
    return False


def _distinctive(
    answer_words: list[str], text: str, passages: list[tuple[Passage, Hit]]
) -> bool:
    """Does this short answer carry something specific enough to trace?

    A figure qualifies — a headcount, an amount, a date, a code.

    So does a name, which the digit test alone used to miss. Asked "Harry owl
    name" the store answered "Hedwig", correctly, off a passage sitting
    directly beneath it, and the UI stamped the answer "not supported by the
    collection" in red because a name contains no digit. Names, places and
    product identifiers are the other large class of short factual answer, and
    the warning firing on all of them is the warning firing on nothing.

    A capital letter alone would be far too weak — "Yes", "The second one" and
    "Receipts must be" are capitalised too, purely by position. So a candidate
    has to clear two bars, and neither is negotiable:

      used as a name   It appears capitalised MID-SENTENCE in the passage, not
                       merely at the start of one. See _used_as_a_name.

      unambiguous      Exactly one of the retrieved passages uses it that way.
                       If two do there is no way to tell which was read, and
                       the caller's whole contract is that a citation points
                       somewhere checkable.

    Both calibrate against the collection in front of them rather than against
    a stop-word list nobody maintains: "Hedwig" is a name in one passage of
    five; "yes" is a name in none of them, and nobody had to enumerate it.
    """
    if any(char.isdigit() for word in answer_words for char in word):
        return True
    for word in dict.fromkeys(_CAPITALISED.findall(text)):
        if sum(1 for passage, _ in passages if _used_as_a_name(passage.text, word)) == 1:
            return True
    return False


def _attribute_short(
    answer_words: list[str], text: str, passages: list[tuple[Passage, Hit]]
) -> Citation | None:
    """Attribute an answer too short to have a seven-word run in it.

    Asked for a headcount the model replied "96" — right, straight off the
    passage on screen beneath it — and the answer was stamped "not supported by
    the collection", because a one-word answer cannot contain a seven-word
    shingle. Short factual answers are the commonest kind there is: a number, a
    date, a name. A warning that fires on all of them is a warning nobody reads.

    So a short answer is matched on its words instead, and only when the match
    is UNAMBIGUOUS: every word of it present in exactly one passage. If two
    passages both contain "96" there is no way to tell which was used, and
    guessing would attach a checkable-looking reference to the wrong place —
    which is worse than leaving it uncited.

    It also needs something DISTINCTIVE to match on — a figure or a name; see
    _distinctive. A short answer made only of ordinary words — "Receipts must
    be", "yes", "the second one" — shares those words with half the collection,
    so matching on them would credit a passage that merely uses the same
    vocabulary. Those stay uncited, which is the case the strict rule was
    written for in the first place.

    Whole words, not substrings: "96" must not match "960".
    """
    if not _distinctive(answer_words, text, passages):
        return None

    found: list[tuple[int, Passage, Hit]] = []
    for index, (passage, hit) in enumerate(passages, start=1):
        if set(answer_words) <= set(_words(passage.text)):
            found.append((index, passage, hit))

    if len(found) != 1:
        return None
    index, passage, hit = found[0]
    return Citation(
        marker=index,
        chunk_id=passage.chunk_id,
        item_id=hit.item_id,
        title=hit.title,
        heading=passage.heading,
        text=passage.text,
        score=passage.score,
        page=passage.page,
        regions=passage.regions,
    )


def _attribute(text: str, passages: list[tuple[Passage, Hit]]) -> Citation | None:
    """Find the passage an uncited answer was actually drawn from.

    Verbatim overlap only. A paraphrase that shares no run of wording is
    indistinguishable here from an invention, and guessing between them is
    exactly what this whole path exists to avoid — so it stays uncited and the
    answer is reported ungrounded.
    """
    answer_words = _words(text)
    if len(answer_words) < _SHINGLE:
        return _attribute_short(answer_words, text, passages)
    shingles = {
        " ".join(answer_words[i : i + _SHINGLE])
        for i in range(len(answer_words) - _SHINGLE + 1)
    }

    for index, (passage, hit) in enumerate(passages, start=1):
        haystack = " ".join(_words(passage.text))
        if any(shingle in haystack for shingle in shingles):
            return Citation(
                marker=index,
                chunk_id=passage.chunk_id,
                item_id=hit.item_id,
                title=hit.title,
                heading=passage.heading,
                text=passage.text,
                score=passage.score,
                page=passage.page,
                regions=passage.regions,
            )
    return None


async def _attach_pages(
    session: AsyncSession, scope: Scope, citations: list[Citation]
) -> None:
    """Give every citation the page it came off, where the document has pages.

    Passages cut by the chunker carry no page — only the page index knows that
    — so a citation showed its page under vectorless and showed nothing under
    hybrid. Same passage, same document, different story depending on a
    retrieval choice the reader never made.

    Looked up here rather than stored on the chunk because it needs no schema
    change and cannot go stale: the page is derived from the body the passage
    was cut from, so it is right by construction.
    """
    from packages.core import tree
    from packages.core.store import get_items_by_ids

    wanted = [c for c in citations if c.page is None]
    if not wanted:
        return
    from packages.core import pages

    items = await get_items_by_ids(session, scope, sorted({c.item_id for c in wanted}))
    # Only documents whose pages can actually be RENDERED get a page number.
    # A slide deck writes the same page markers a PDF does, so it looked like
    # it had pages — and the citation offered a picture of slide 3 that nothing
    # can produce, which the console rendered as a broken image. A promise the
    # store cannot keep is worse than no promise.
    bodies = {
        item.id: item.body
        for item in items
        if pages.renderable("", item.source.locator or "")
    }
    for citation in wanted:
        body = bodies.get(citation.item_id)
        if body:
            citation.page = tree.page_containing(body, citation.text)


async def _write(
    question: str,
    passages: list[tuple[Passage, Hit]],
    emit: EmitFn | None,
) -> str:
    """Write the grounded answer from the passages.

    When someone is watching, the answer is streamed token by token as the
    model produces it and the full text is returned all the same, so the
    citation resolution downstream is identical either way. Unwatched, it is the
    one blocking call it always was. The prompt and rules do not change — only
    whether the writing is shown as it happens.
    """
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _prompt(question, passages)},
    ]
    if emit is None:
        from packages.core.llm import chat

        return await asyncio.to_thread(chat, messages, temperature=0.0)

    from packages.core.llm import astream_chat_with_tools

    parts: list[str] = []
    async for event in astream_chat_with_tools(messages, [], temperature=0.0):
        if event["type"] == "text":
            parts.append(event["delta"])
            await emit({"type": "token", "delta": event["delta"]})
        elif event["type"] == "error":
            raise RuntimeError(event["error"])
        elif event["type"] == "done" and not parts:
            # Some providers deliver the whole message in the final frame with no
            # incremental text; take it so a non-incremental provider still works.
            parts.append(event["content"])
    return "".join(parts)


async def answer(
    session: AsyncSession,
    scope: Scope,
    question: str,
    cfg: RetrievalConfig = DEFAULT,
    mode: str = "agentic",
    emit: EmitFn | None = None,
    item_ids: tuple[str, ...] = (),
) -> tuple[Answer, Trace]:
    """Retrieve, then write an answer from what was retrieved.

    ``item_ids`` restricts every mode to the named documents. It is applied at
    the retrieval layer, never by filtering results afterwards: a filter that
    discards hits after ranking gives you the best of everything and then throws
    most of it away, so a question scoped to one document would end up answered
    from whatever fraction of it happened to place in the global top ten.

    Two public modes, and the writing is identical for both:

      agentic  (default) an agent reads its way to the answer. It reasons over
               each document's table of contents, runs hybrid_search to reach a
               figure or identifier no heading advertises, and hops the
               similarity graph from a promising passage — reaching the whole
               collection and choosing its own strategy per question.
      hybrid   passage embeddings and keyword matching, fused into one ranked
               pass. No agent loop: fast, deterministic, and the retrieval
               primitive other software builds on.

    Agentic is the default because it reaches the whole store and picks how to
    find the evidence; hybrid stays a request away for callers that need speed
    and a reproducible ranking.

    A third value, ``vectorless``, is accepted for backward compatibility only
    and is no longer offered as a choice. It is agentic's catalogue-reasoning
    step with search and graph-hop turned off — the same table-of-contents
    navigation, but blind to anything past the newest ``MAX_DOCUMENTS``
    documents, so a large collection answers from a partial view (the navigator
    now says so on the answer). Prefer agentic, which does the same reasoning
    and can also reach the rest.

    Returns the trace alongside, so the answer and the retrieval that produced
    it can be recorded together — an answer whose retrieval cannot be inspected
    is not one anybody can argue with.
    """
    started = time.perf_counter()

    if mode in ("vectorless", "agentic"):
        # The navigator answers as it reads, so there is no second pass here.
        # Splitting "choose sections" from "write an answer" is what made the
        # old version brittle: the choosing step had to commit before seeing
        # any content, and when it chose nothing the store looked empty.
        #
        # Agentic is the same loop with one more tool: the agent reasons over
        # the table of contents AND may run hybrid search to locate a figure a
        # heading would never advertise. The post-processing below is identical
        # either way — evidence is credited by what the loop actually read.
        from packages.core.navigator import navigate

        walk, trace = await navigate(
            session, scope, question, hybrid=(mode == "agentic"), emit=emit, only=item_ids
        )
        result = Answer(
            question=question,
            text=walk.answer,
            hits=walk.hits,
            trace_id=trace.trace_id,
            degraded=walk.degraded,
            mode=mode,
            # Set before any early return: a walk that read three sections and
            # still found nothing is the case where the route matters MOST, and
            # it is exactly the case an "answer only" field would drop.
            steps=[
                {"round": s.round, "action": s.action, "detail": s.detail}
                for s in walk.steps
            ],
        )
        passages = _gather(walk.hits, limit=MAX_PASSAGES)
        if not passages:
            result.grounded = False
            result.text = walk.answer or "Nothing in these documents answers that."
            result.took_ms = int((time.perf_counter() - started) * 1000)
            return result, trace

        # The agent was handed [1], [2] … in the order it read them, so the
        # markers resolve against exactly what it opened.
        result.text, result.citations = _resolve_citations(walk.answer, passages)
        if not result.citations:
            attributed = _attribute(result.text, passages)
            if attributed is not None:
                result.citations = [attributed]

        # Still nothing, but the agent DID read sections and reported that it
        # answered from them. Here that is not an inference: the loop records
        # what it opened, and an answer written after reading exactly those
        # sections came from exactly those sections. A synthesis across a
        # section shares no verbatim run with it and writes no marker, so both
        # earlier checks miss — and the result was a correct, sourced answer
        # stamped "not supported by the collection".
        #
        # This holds only for the navigator, whose evidence is structural.
        # Hybrid hands the model a pile of candidate passages and cannot know
        # which it leaned on, so it keeps the stricter rule.
        if not result.citations and walk.found:
            result.citations = [
                Citation(
                    marker=index,
                    chunk_id=passage.chunk_id,
                    item_id=hit.item_id,
                    title=hit.title,
                    heading=passage.heading,
                    text=passage.text,
                    score=passage.score,
                    page=passage.page,
                    regions=passage.regions,
                )
                for index, (passage, hit) in enumerate(passages, start=1)
            ]
        result.grounded = bool(result.citations) and walk.found
        # Sections the page index cut already know their page; a section the
        # chunker cut does not. Both end up here, so both are filled in.
        await _attach_pages(session, scope, result.citations)
        result.took_ms = int((time.perf_counter() - started) * 1000)
        return result, trace
    else:
        # Hybrid used to stream nothing at all until the first word of the
        # answer — measured at 1.3 to 4.3 seconds of a spinner that said
        # "searching…" and never changed. The work is real and it is
        # describable, so it is described: a progress line that never moves is
        # indistinguishable from a hang, and the whole point of offering a
        # streamed delivery was to stop making people guess.
        #
        # Three lines cover that stretch, and all three ride the emit protocol
        # rather than a second step channel: `start` (the client renders it as
        # "Searching passages…"), `retrieved` below (documents and passages
        # matched), and the writing line further down.
        if emit is not None:
            await emit({"type": "start", "mode": mode})
        if item_ids:
            # Pushed into the config so it reaches BOTH arms of the fusion —
            # the vector query and the keyword query filter alike. Narrowing
            # only one of them would let the other quietly reintroduce a
            # document the caller excluded.
            cfg = replace(cfg, item_ids=tuple(item_ids))
        hits, trace = await search_traced(session, scope, question, cfg)
        degraded = trace.degraded

    result = Answer(
        question=question,
        text="",
        hits=hits,
        trace_id=trace.trace_id,
        degraded=degraded,
        mode=mode,
    )

    passages = _gather(hits)
    if emit is not None:
        # What the fusion returned, before a word is written — the evidence the
        # answer is about to be built from, shown as it is found.
        await emit(
            {
                "type": "retrieved",
                "documents": len({hit.item_id for _, hit in passages}),
                "passages": len(passages),
            }
        )
    if not passages:
        # An empty context is where a language model invents most confidently,
        # so it is not asked at all.
        result.grounded = False
        result.text = (
            "Nothing in this collection answers that. Try different wording, or add "
            "a document that covers it."
            if mode != "vectorless"
            else "No section of these documents looks like it answers that."
        )
        result.took_ms = int((time.perf_counter() - started) * 1000)
        return result, trace

    # The last silent stretch: retrieval has reported, and the first token is
    # still a model round-trip away. Hybrid has no route to narrate — it ranks
    # and hands over — so the only thing left to announce is the writing.
    # Vectorless has already been narrating tool by tool and does not need it.
    if mode != "vectorless" and emit is not None:
        await emit(
            {
                "type": "tool_call",
                "tool": "write_answer",
                "args": {"passages": len(passages)},
                "round": 1,
            }
        )

    try:
        raw = await _write(question, passages, emit)
    except Exception as exc:
        # The passages are still worth having. Saying the answer is missing is
        # far better than quietly returning search results as though they were
        # what was asked for.
        result.degraded = _join(
            result.degraded, f"answer unavailable: {type(exc).__name__}"
        )
        result.grounded = False
        result.text = (
            "The passages below were found, but the answer could not be written — "
            "the language model was unreachable."
        )
        result.took_ms = int((time.perf_counter() - started) * 1000)
        return result, trace

    raw = (raw or "").strip()
    if raw.startswith(_NOT_FOUND):
        # Not answered — but the explanation of what IS there often cites the
        # passages it looked at ("they mention a cost model [1] but no figure").
        # Those citations are resolved like any other, so the reader can follow
        # the near-misses. Only `grounded` says the question went unanswered;
        # the evidence stays reachable either way.
        remainder = raw[len(_NOT_FOUND) :].strip(" :-—\n")
        text, citations = _resolve_citations(remainder, passages)
        result.grounded = False
        result.citations = citations
        result.text = text or "The passages found do not answer that question."
        result.took_ms = int((time.perf_counter() - started) * 1000)
        return result, trace

    result.text, result.citations = _resolve_citations(raw, passages)

    # A missing marker is not the same as missing evidence. Asked "how long do
    # I have to submit receipts", the model answered by quoting the passage
    # word for word and simply did not write [1] — and a rule that trusted the
    # marker labelled a correct, sourced answer "not supported by the
    # collection". A warning that fires on good answers is a warning people
    # learn to ignore.
    #
    # So an uncited answer is checked against the passages rather than assumed
    # baseless: if its wording is actually present in one of them, that passage
    # is the citation. Prose matching nothing stays ungrounded, which is the
    # case this was always for.
    if not result.citations:
        found = _attribute(result.text, passages)
        if found is not None:
            result.citations = [found]

    # A marker is not evidence. `grounded` used to mean "the model wrote [3]
    # and [3] resolved", which is a check on formatting — and it passed the
    # worst answer this store has produced: "Mercury (Hg) is liquid at room
    # temperature [3]", cited to a periodic table that names mercury and never
    # says "liquid". True in the world, absent from the source, and stamped
    # grounded.
    #
    # So the answer is now read back against the passages it cites. Most cost
    # nothing — the lexical gate in core.support settles them — and only an
    # answer that introduces vocabulary its own source never uses is worth a
    # second call.
    if result.citations:
        verdict = await support.check(
            result.text, "\n\n".join(c.text for c in result.citations)
        )
        if not verdict.supported:
            result.grounded = False
            result.degraded = _join(
                result.degraded,
                "not supported by the cited passages"
                + (f": {verdict.reason}" if verdict.reason else ""),
            )
        else:
            result.grounded = True
    else:
        result.grounded = False
    await _attach_pages(session, scope, result.citations)
    result.took_ms = int((time.perf_counter() - started) * 1000)
    return result, trace


def _join(existing: str | None, addition: str) -> str:
    return f"{existing}; {addition}" if existing else addition


__all__ = ["MAX_PASSAGES", "Answer", "Citation", "answer"]
