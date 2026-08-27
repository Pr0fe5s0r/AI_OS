"""03 · Search filters & traces — scope a search, then see why it ranked.

Shows: restricting to specific documents (`files=`), to connectors (`sources=`),
a score floor (`min_score=`), and pulling the retrieval trace behind a result.

    python python/03_search_filters.py "refund policy"
"""
from __future__ import annotations

import sys

from markvector import Markvector, MarkvectorError


def main(query: str) -> None:
    with Markvector() as mv:
        docs = mv.collection("cookbook")

        # 1) Whole collection, but drop weak matches with a score floor.
        print("# whole collection (min_score=0.2)")
        results = docs.search(query, limit=10, min_score=0.2)
        for hit in results:
            print(f"  {hit.score:.3f}  {hit.title}  ({hit.matched_on})")

        # 2) Only inside the three newest uploaded files. A scoped search never
        #    returns a document outside the set you pass.
        picked = docs.files(limit=3)
        if picked:
            print(f"\n# scoped to {len(picked)} files")
            for hit in docs.search(query, files=picked):
                print(f"  {hit.score:.3f}  {hit.title}")

        # 3) Only from a particular connector (text via add() defaults to
        #    source='sdk'; uploads default to 'upload').
        print("\n# scoped to source='upload'")
        for hit in docs.search(query, sources=["upload"]):
            print(f"  {hit.score:.3f}  {hit.source.source}  {hit.title}")

        # 4) Why did the first search rank the way it did? Every search carries a
        #    trace_id; hand it back for each arm's candidates, scores and timing.
        if results.trace_id:
            print(f"\n# trace {results.trace_id}  (took {results.took_ms} ms)")
            trace = mv.trace(results.trace_id)
            print("  keys:", ", ".join(trace.keys()))


if __name__ == "__main__":
    try:
        main(" ".join(sys.argv[1:]) or "refund policy")
    except MarkvectorError as exc:
        raise SystemExit(f"markvector error: {exc}") from exc
