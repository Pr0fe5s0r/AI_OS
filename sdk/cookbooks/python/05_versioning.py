"""05 · Versioning — update a document in place and walk its history.

Writing the same `locator` again does not add a copy; it supersedes the previous
version. Old versions stay addressable, so you get an audit trail for free.

    python python/05_versioning.py
"""
from __future__ import annotations

from markvector import Markvector, MarkvectorError

LOCATOR = "policy/refunds"


def main() -> None:
    with Markvector() as mv:
        docs = mv.collection("cookbook")

        # v1
        docs.add(
            "Refunds are issued within 30 days of purchase.",
            locator=LOCATOR,
            title="Refund policy",
            wait=True,
        )
        # v2 — same locator, so this supersedes v1 rather than duplicating it.
        docs.add(
            "Refunds are issued within 14 days of purchase. Digital goods excluded.",
            locator=LOCATOR,
            title="Refund policy",
            wait=True,
        )

        # Find the live document for this locator.
        current = next(d for d in docs.list(limit=200) if d.source.locator == LOCATOR)
        print(f"current: v{current.version}  {current.body}")

        # Every version, newest first.
        print("\nhistory:")
        for v in docs.versions(current.id):
            print(f"  v{v.version}  {v.status:10}  {v.body[:60]}…")

        # Fetch a specific old version by number.
        first = docs.get(current.id, version=1)
        print(f"\nv1 said: {first.body}")


if __name__ == "__main__":
    try:
        main()
    except MarkvectorError as exc:
        raise SystemExit(f"markvector error: {exc}") from exc
