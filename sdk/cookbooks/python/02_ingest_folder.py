"""02 · Ingest a folder — bulk-upload every PDF/.docx/.md/.txt in a directory.

Re-running is safe: each file's path is used as its `locator`, so a second run
updates the existing document instead of adding a duplicate.

    python python/02_ingest_folder.py ./reports
"""
from __future__ import annotations

import sys
from pathlib import Path

from markvector import Markvector, MarkvectorError

SUPPORTED = {".pdf", ".docx", ".md", ".txt"}


def main(folder: str) -> None:
    root = Path(folder)
    files = sorted(p for p in root.rglob("*") if p.suffix.lower() in SUPPORTED)
    if not files:
        raise SystemExit(f"No PDF/.docx/.md/.txt files under {root}")

    with Markvector() as mv:
        docs = mv.collection("cookbook")

        # Kick off every upload without waiting, then wait on the last one — by
        # the time it is searchable, the earlier ones almost certainly are too.
        for i, path in enumerate(files):
            last = i == len(files) - 1
            # locator = path relative to the folder, so the same file always
            # maps to the same document across runs.
            locator = str(path.relative_to(root)).replace("\\", "/")
            result = docs.add_file(path, locator=locator, wait=last)
            print(f"  {'indexed' if result.indexed else 'queued ':8}  {locator}")

        print(f"\n{len(files)} files in collection {docs.id!r}:")
        for doc in docs.files():
            size = doc.original.size if doc.original else 0
            print(f"  {doc.id}  {doc.source.locator}  ({size} bytes)")


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else ".")
    except MarkvectorError as exc:
        raise SystemExit(f"markvector error: {exc}") from exc
