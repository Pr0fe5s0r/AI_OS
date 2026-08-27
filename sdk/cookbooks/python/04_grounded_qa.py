"""04 · Grounded Q&A — a cited answer, and how to trust it.

`answer.grounded` is the whole point: it is False when the store had nothing to
answer from, so you can refuse to show a confident-sounding guess. This recipe
also contrasts the two retrieval modes, and prints the two things that let a
reader check the answer rather than take it on trust: where on the page each
citation was read from, and the route the answer took to get there.

    python python/04_grounded_qa.py "what does the contract say about termination?"
"""
from __future__ import annotations

import sys

from markvector import Markvector, MarkvectorError


def ask(docs, question: str, mode: str) -> None:
    ans = docs.answer(question, mode=mode)
    print(f"\n# {mode}  (grounded={ans.grounded}, {ans.took_ms} ms)")
    if not ans.grounded:
        # Do not print ans.text here — an ungrounded answer is a guess.
        print("  The knowledge base does not cover this. Not answering.")
        return
    print(" ", ans.text)
    print("  sources:")
    for c in ans.citations:
        print(f"    [{c.marker}] {c.title} — {c.heading}")
        print(f"        {c.text[:100]}…")
        # `page` is set only when the passage was read off a PICTURE of the page
        # rather than out of its text, and `regions` say where on it. Draw those
        # boxes and a transcribed table becomes checkable against the original
        # instead of merely plausible. Both are empty for text passages, which is
        # the normal case, not a failure.
        if c.page is not None:
            print(f"        page {c.page}")
            for r in c.regions:
                # Percentages of the page, so they survive whatever size you
                # render it at: multiply by your rendered width and height.
                print(f"          box {r.x},{r.y} {r.w}×{r.h} — {r.label}")

    # The route taken to get here — sections opened, ids reached for and missed,
    # where it stopped. It rides on the answer rather than sitting in the trace
    # because "why should I believe this" is answered by the route, and nobody
    # goes and opens a trace. Empty for hybrid, which ranks and hands over.
    if ans.steps:
        print("  route:")
        for step in ans.steps:
            print(f"    {step}")


def main(question: str) -> None:
    with Markvector() as mv:
        docs = mv.collection("cookbook")
        # agentic (default): an agent reasons over the heading trees, searches
        # passages, and hops the similarity graph — reaching the whole collection.
        ask(docs, question, mode="agentic")
        # hybrid: passage embeddings + keyword, fused. Fast and deterministic;
        # try both on hard questions.
        ask(docs, question, mode="hybrid")


if __name__ == "__main__":
    try:
        main(" ".join(sys.argv[1:]) or "summarise Q2 paid performance")
    except MarkvectorError as exc:
        raise SystemExit(f"markvector error: {exc}") from exc
