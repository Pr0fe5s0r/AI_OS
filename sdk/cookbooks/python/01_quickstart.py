"""01 · Quickstart — create a collection, add text, search, answer.

    export MARKVECTOR_API_KEY=kb_live_…
    python python/01_quickstart.py
"""
from __future__ import annotations

from markvector import Markvector, MarkvectorError


def main() -> None:
    with Markvector() as mv:                       # reads MARKVECTOR_API_KEY / _URL
        print("workspace:", mv.whoami())

        # A place to work. The id is derived from the name when omitted; passing
        # it explicitly makes this script safe to re-run (collection() never
        # calls the server, and re-creating a known id is a no-op).
        info = mv.create_collection("Cookbook demo", collection_id="cookbook")
        docs = mv.collection(info.collection_id)

        # Write, and block until it is actually searchable (indexing is async).
        docs.add(
            "Paid conversions fell 18 percent in Q2, driven by a CPC increase.",
            locator="notes/q2",
            title="Q2 note",
            wait=True,
        )

        # Search — meaning and exact wording, fused into one ranking.
        print("\n# search")
        for hit in docs.search("why did paid results drop", limit=5):
            print(f"  {hit.score:.3f}  {hit.title}  ({hit.matched_on})")
            print(f"    {hit.clean_excerpt}")

        # Ask — a written answer built ONLY from what was retrieved.
        print("\n# answer")
        answer = docs.answer("what happened to paid conversions in Q2?")
        if answer.grounded:
            print(" ", answer.text)
            for c in answer.citations:
                print(f"    [{c.marker}] {c.title} — {c.heading}")
        else:
            print("  Not enough in the store to answer that yet.")


if __name__ == "__main__":
    try:
        main()
    except MarkvectorError as exc:
        raise SystemExit(f"markvector error: {exc}") from exc
