from __future__ import annotations

import asyncio
import re
import time
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.text,
            "grounded": self.grounded,
            "degraded": self.degraded,
            "trace_id": self.trace_id,
            "took_ms": self.took_ms,
            "citations": [
                {
                    "marker": c.marker,
                    "chunk_id": c.chunk_id,
                    "item_id": c.item_id,
                    "title": c.title,
                    "heading": c.heading,
                    "text": c.text,
                    "score": c.score,
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
        )

    def keep(match: re.Match[str]) -> str:
        return match.group(0) if int(match.group(1)) in used else ""

    cleaned = _CITATION.sub(keep, text)
    # Tidy the spacing a removed marker leaves behind.
    cleaned = re.sub(r" +([.,;:])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned, [used[m] for m in sorted(used)]


async def answer(
    session: AsyncSession,
    scope: Scope,
    question: str,
    cfg: RetrievalConfig = DEFAULT,
) -> tuple[Answer, Trace]:
    """Retrieve, then write an answer from what was retrieved.

    Returns the trace alongside, so the answer and the retrieval that produced
    it can be recorded together — an answer whose retrieval cannot be inspected
    is not one anybody can argue with.
    """
    started = time.perf_counter()
    hits, trace = await search_traced(session, scope, question, cfg)

    result = Answer(
        question=question,
        text="",
        hits=hits,
        trace_id=trace.trace_id,
        degraded=trace.degraded,
    )

    passages = _gather(hits)
    if not passages:
        # An empty context is where a language model invents most confidently,
        # so it is not asked at all.
        result.grounded = False
        result.text = (
            "Nothing in this collection answers that. Try different wording, or add "
            "a document that covers it."
        )
        result.took_ms = int((time.perf_counter() - started) * 1000)
        return result, trace

    from packages.core.llm import chat

    try:
        raw = await asyncio.to_thread(
            chat,
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _prompt(question, passages)},
            ],
            temperature=0.0,
        )
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
    # An answer that cites nothing is not grounded, whatever it claims. This is
    # the case worth catching: confident prose with no evidence behind it.
    result.grounded = bool(result.citations)
    result.took_ms = int((time.perf_counter() - started) * 1000)
    return result, trace


def _join(existing: str | None, addition: str) -> str:
    return f"{existing}; {addition}" if existing else addition


__all__ = ["MAX_PASSAGES", "Answer", "Citation", "answer"]
