from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.search import DEFAULT, RetrievalConfig, Trace, search_traced
from packages.shared.schema import Hit, Passage, Scope

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
    mode: str = "vectorless"

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


def _attribute_short(
    answer_words: list[str], passages: list[tuple[Passage, Hit]]
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

    It also needs something DISTINCTIVE to match on, which in practice means a
    figure: a headcount, an amount, a date, a code. A short answer made only of
    ordinary words — "Receipts must be", "yes", "the second one" — shares those
    words with half the collection, so matching on them would credit a passage
    that merely uses the same vocabulary. Those stay uncited, which is the case
    the strict rule was written for in the first place.

    Whole words, not substrings: "96" must not match "960".
    """
    if not any(char.isdigit() for word in answer_words for char in word):
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
        return _attribute_short(answer_words, passages)
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


async def answer(
    session: AsyncSession,
    scope: Scope,
    question: str,
    cfg: RetrievalConfig = DEFAULT,
    mode: str = "vectorless",
    on_step: Callable[[Any], None] | None = None,
    on_token: Callable[[str], None] | None = None,
) -> tuple[Answer, Trace]:
    """Retrieve, then write an answer from what was retrieved.

    Two ways of retrieving, and the writing is identical either way:

      vectorless  reason over each document's table of contents and open the
                  sections that look like they answer the question (default)
      hybrid      passage embeddings and keyword matching, fused

    Vectorless is the default because on the material this store actually
    holds — long documents with headings their authors wrote on purpose — it
    was measurably faster and cited more precisely. That is a default, not a
    verdict: hybrid remains a request away, and is the better choice for flat
    text or for finding an identifier buried anywhere in a corpus.

    They suit different material. Hybrid is better at finding a specific figure
    or identifier anywhere in a corpus; vectorless is better on long structured
    documents where the author already labelled what is where, and where
    similarity keeps returning passages that sound right and are not. Neither
    is a strict improvement, which is why this is a choice rather than a
    replacement.

    Returns the trace alongside, so the answer and the retrieval that produced
    it can be recorded together — an answer whose retrieval cannot be inspected
    is not one anybody can argue with.
    """
    started = time.perf_counter()

    if mode == "vectorless":
        # The navigator answers as it reads, so there is no second pass here.
        # Splitting "choose sections" from "write an answer" is what made the
        # old version brittle: the choosing step had to commit before seeing
        # any content, and when it chose nothing the store looked empty.
        from packages.core.navigator import navigate

        walk, trace = await navigate(
            session, scope, question, on_step=on_step, on_token=on_token
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

    from packages.core.llm import chat, stream_chat_with_tools

    written = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _prompt(question, passages)},
    ]

    def _stream() -> str:
        """Hybrid has no route to narrate — it ranks and hands over — so the
        only thing to stream is the writing itself."""
        out: list[str] = []
        for event in stream_chat_with_tools(written, [], temperature=0.0):
            if event["type"] == "text":
                out.append(event["delta"])
                if on_token is not None:
                    on_token(event["delta"])
            elif event["type"] == "done" and not out:
                out.append(event["content"])
        return "".join(out)

    try:
        if on_token is not None:
            raw = await asyncio.to_thread(_stream)
        else:
            raw = await asyncio.to_thread(chat, written, temperature=0.0)
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
    result.grounded = bool(result.citations)
    await _attach_pages(session, scope, result.citations)
    result.took_ms = int((time.perf_counter() - started) * 1000)
    return result, trace


def _join(existing: str | None, addition: str) -> str:
    return f"{existing}; {addition}" if existing else addition


__all__ = ["MAX_PASSAGES", "Answer", "Citation", "answer"]
