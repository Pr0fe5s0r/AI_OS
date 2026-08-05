"""06 · Structure (PageIndex) — the heading tree, and the chunks a doc became.

`structure()` returns a document's own table of contents (built from its
headings, no model calls) — this is what vectorless search reasons over.
`chunks()` returns the passages the document was split into — what embedding
search actually matches against. Two views of the same document.

    python python/06_structure.py            # picks the first uploaded file
"""
from __future__ import annotations

from markvector import Markvector, MarkvectorError


def main() -> None:
    with Markvector() as mv:
        docs = mv.collection("cookbook")

        picked = docs.files(limit=1) or docs.list(limit=1)
        if not picked:
            raise SystemExit("Collection is empty — run 01 or 02 first.")
        doc = picked[0]

        # The heading tree. `sections` is the top level; `walk()` flattens all.
        tree = docs.structure(doc)
        print(f"# structure of {tree.title!r} — {tree.nodes} sections")
        for section in tree.walk():
            print(f"  ~{section.tokens:>5} tok  {section.title}")
            if section.opens:
                print(f"              {section.opens}")

        # The passages it was indexed as — the unit of retrieval.
        print("\n# chunks")
        for chunk in docs.chunks(doc.id):
            print(f"  #{chunk.ordinal + 1}  {chunk.heading}")
            print(f"      {chunk.text[:100]}…")

        # Bulk: pull the structure of many files in one round trip.
        many = docs.files(limit=5)
        if len(many) > 1:
            print(f"\n# structures of {len(many)} files (one call)")
            for s in docs.structures(many):
                print(f"  {s.item_id}  {s.nodes} sections  {s.title}")


if __name__ == "__main__":
    try:
        main()
    except MarkvectorError as exc:
        raise SystemExit(f"markvector error: {exc}") from exc
