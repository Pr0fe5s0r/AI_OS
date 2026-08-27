"""09 · Categorise — pin a document's category, then list by it.

A pinned category sticks: re-ingesting the document will not overwrite a choice
you made by hand. Passing `category=` to `list()` filters to a single class.

    python python/09_categorise.py
"""
from __future__ import annotations

from markvector import Markvector, MarkvectorError


def main() -> None:
    with Markvector() as mv:
        docs = mv.collection("cookbook")

        picked = docs.list(limit=1)
        if not picked:
            raise SystemExit("Collection is empty — run 01 or 02 first.")
        doc = picked[0]

        # File it by hand under one or more classes.
        docs.categorise(doc.id, ["legal", "reviewed-2026"])
        print(f"filed {doc.id} under legal, reviewed-2026")

        # The choice is now visible on the document, marked pinned.
        refreshed = docs.get(doc.id)
        for c in refreshed.categories:
            flag = "pinned" if c.pinned else f"auto {c.confidence:.2f}"
            print(f"  {c.class_id}  ({flag})")

        # List everything in a single class.
        print("\ndocuments in 'legal':")
        for d in docs.list(category="legal"):
            print(f"  {d.id}  {d.title}")


if __name__ == "__main__":
    try:
        main()
    except MarkvectorError as exc:
        raise SystemExit(f"markvector error: {exc}") from exc
