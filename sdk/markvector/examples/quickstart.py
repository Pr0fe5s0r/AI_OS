"""End-to-end tour of the markvector SDK.

Run against a live MarkVector API:

    export MARKVECTOR_API_KEY=kb_live_…      # from the console, Developer → API keys
    export MARKVECTOR_URL=http://tnega-api-o9ecgm-5fbf26-217-154-175-169.traefik.me
    python examples/quickstart.py
"""
from __future__ import annotations

from markvector import Markvector, MarkvectorError


def main() -> None:
    with Markvector() as mv:                        # reads MARKVECTOR_API_KEY / _URL
        print("workspace:", mv.whoami())

        # A collection to work in (id is derived from the name if omitted).
        info = mv.create_collection("SDK demo", collection_id="sdk-demo")
        docs = mv.collection(info.collection_id)

        # Write some text and wait until it is actually searchable.
        docs.add(
            "Paid conversions fell 18 percent in Q2, driven by a CPC increase.",
            locator="notes/q2",
            title="Q2 note",
            wait=True,
        )

        # Search — by meaning and wording together.
        print("\n# search")
        for hit in docs.search("why did paid results drop", limit=5):
            print(f"  {hit.score:.3f}  {hit.title}  ({hit.matched_on})")

        # Ask — a grounded, cited answer.
        print("\n# answer")
        answer = docs.answer("what happened to paid conversions in Q2?")
        print("  grounded:", answer.grounded)
        print(" ", answer.text)
        for c in answer.citations:
            print(f"    [{c.marker}] {c.title} — {c.heading}")

        # The passages the document was split into.
        print("\n# chunks")
        doc = docs.list(limit=1)[0]
        for chunk in docs.chunks(doc.id):
            print(f"  #{chunk.ordinal + 1}  {chunk.heading}: {chunk.text[:80]}…")


if __name__ == "__main__":
    try:
        main()
    except MarkvectorError as exc:
        raise SystemExit(f"markvector error: {exc}") from exc
