"""10 · Download originals — get uploaded files back byte-for-byte.

MarkVector keeps the original bytes of anything uploaded via `add_file`, so you
can retrieve the exact file that came in — not the Markdown it was converted to.
Text written via `add()` has no original and raises NotFound.

    python python/10_download_originals.py ./downloaded
"""
from __future__ import annotations

import sys
from pathlib import Path

from markvector import Markvector, MarkvectorError, NotFound


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    with Markvector() as mv:
        docs = mv.collection("cookbook")

        uploads = docs.files()          # only docs that have a downloadable original
        if not uploads:
            raise SystemExit("No uploaded files — run 02_ingest_folder first.")

        for doc in uploads:
            name = doc.original.filename
            try:
                # Pass path= to stream straight to disk; omit it to get bytes.
                written = docs.download_original(doc.id, path=out / name)
                print(f"  {written}  ({doc.original.size} bytes, {doc.original.content_type})")
            except NotFound:
                print(f"  {doc.id}: no stored original, skipped")


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "./downloaded")
    except MarkvectorError as exc:
        raise SystemExit(f"markvector error: {exc}") from exc
