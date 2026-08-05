"""04 · Grounded Q&A — a cited answer, and how to trust it.

`answer.grounded` is the whole point: it is False when the store had nothing to
answer from, so you can refuse to show a confident-sounding guess. This recipe
also contrasts the two retrieval modes.

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


def main(question: str) -> None:
    with Markvector() as mv:
        docs = mv.collection("cookbook")
        # vectorless (default): reason over each document's heading tree.
        ask(docs, question, mode="vectorless")
        # hybrid: passage embeddings + keyword, fused. Try both on hard questions.
        ask(docs, question, mode="hybrid")


if __name__ == "__main__":
    try:
        main(" ".join(sys.argv[1:]) or "summarise Q2 paid performance")
    except MarkvectorError as exc:
        raise SystemExit(f"markvector error: {exc}") from exc
