from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Ready-to-paste code, filled in with the caller's own collection.
#
# Generated rather than written into the docs because the two details that
# make a snippet work — which collection, and which host — are exactly the two
# a person has to remember to change, and forgetting is where onboarding ends.
#
# The key is never interpolated. A snippet is copied into chat messages, issue
# trackers and screenshots; it reads the key from the environment instead, so
# copying it can never leak a credential.
# ---------------------------------------------------------------------------

KEY_ENV = "KB_API_KEY"


def _python(base_url: str, collection: str) -> str:
    return f'''# pip install knowledgebase
import os
from knowledgebase import KnowledgeBase

kb = KnowledgeBase(
    api_key=os.environ["{KEY_ENV}"],
    base_url="{base_url}",
)

docs = kb.collection("{collection}")

# Store a document. `locator` is its stable id — writing the same one again
# updates that document instead of creating a duplicate.
docs.add(
    "Paid conversions fell 18 percent in Q2, driven by a paused campaign.",
    locator="notes/q2-summary",
    title="Q2 summary",
    wait=True,          # indexing is asynchronous; wait until it is searchable
)

# Search by meaning and exact wording at once.
results = docs.search("why did paid results fall")
for match in results:
    print(f"{{match.score:.3f}}  {{match.title}}  ({{match.matched_on}})")
    print(f"        {{match.clean_excerpt}}")

# Every search can explain itself.
print(kb.trace(results.trace_id)["timings_ms"])
'''


def _javascript(base_url: str, collection: str) -> str:
    return f'''const KB = "{base_url}";
const headers = {{
  Authorization: `Bearer ${{process.env.{KEY_ENV}}}`,
  "X-Collection": "{collection}",
  "Content-Type": "application/json",
}};

// Store a document.
await fetch(`${{KB}}/api/items`, {{
  method: "POST",
  headers,
  body: JSON.stringify({{
    source: "sdk",
    locator: "notes/q2-summary",
    title: "Q2 summary",
    body: "Paid conversions fell 18 percent in Q2, driven by a paused campaign.",
  }}),
}});

// Search it. Indexing is asynchronous, so a document written a moment ago
// may take a second to appear.
const res = await fetch(
  `${{KB}}/api/search?` + new URLSearchParams({{ q: "why did paid results fall", limit: "5" }}),
  {{ headers }},
);
const {{ results, trace_id, took_ms }} = await res.json();
console.log(`${{results.length}} results in ${{took_ms}}ms`);
for (const m of results) console.log(m.score.toFixed(3), m.title);

// Why those results?
const trace = await fetch(`${{KB}}/api/traces/${{trace_id}}`, {{ headers }}).then((r) => r.json());
console.log(trace.timings_ms);
'''


def _curl(base_url: str, collection: str) -> str:
    return f'''# Store a document
curl -X POST {base_url}/api/items \\
  -H "Authorization: Bearer ${KEY_ENV}" \\
  -H "X-Collection: {collection}" \\
  -H "Content-Type: application/json" \\
  -d '{{"source":"sdk","locator":"notes/q2-summary","title":"Q2 summary",
       "body":"Paid conversions fell 18 percent in Q2."}}'

# Upload a file
curl -X POST {base_url}/api/items/file \\
  -H "Authorization: Bearer ${KEY_ENV}" \\
  -H "X-Collection: {collection}" \\
  -F "file=@report.pdf"

# Search
curl -G {base_url}/api/search \\
  -H "Authorization: Bearer ${KEY_ENV}" \\
  -H "X-Collection: {collection}" \\
  --data-urlencode "q=why did paid results fall" \\
  --data-urlencode "limit=5"
'''


LANGUAGES = ("python", "javascript", "curl")


def build(base_url: str, collection: str) -> dict[str, Any]:
    """Snippets for one collection, in every language we speak."""
    base_url = base_url.rstrip("/")
    return {
        "collection_id": collection,
        "base_url": base_url,
        "key_env": KEY_ENV,
        "install": {"python": "pip install knowledgebase", "javascript": None, "curl": None},
        "snippets": {
            "python": _python(base_url, collection),
            "javascript": _javascript(base_url, collection),
            "curl": _curl(base_url, collection),
        },
    }


__all__ = ["KEY_ENV", "LANGUAGES", "build"]
