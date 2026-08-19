"""Every route the KNOWLEDGE BASE uses is reachable from the SDK.

The SDK is the product for anyone not using the console, so a capability the
KB ships and the SDK cannot reach is a feature only the console has. That
drift is invisible: nothing fails, the endpoint simply goes unused, and the
person who needed it concludes the store cannot do it.

The bar is what the KB USES, not what the API exposes. This repo carries
routes from a wider system than MarkVector, and shipping a client method for
each one would put facets, a review queue and a taxonomy in front of every
integrator while the product itself has no screen for them. Those are listed
below by name: absent on purpose, not by oversight.

A route added later that nobody excused fails this test, which is the point —
it forces the decision to be made rather than defaulted.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from apps.api.authz import matrix

# Workspace administration, done by a signed-in PERSON rather than by software
# holding a key. Deliberately absent from a client library:
#
#   * brands  — console identity, of no use to a program reading documents.
#   * team    — membership and invitations. An invitation is an email flow
#               ending at a link a human clicks; `accept` is performed BY the
#               invited person, who by definition has no key yet.
#
# Listed here rather than merely missing, so the next person can tell a
# decision from an oversight.
BY_DESIGN = {
    ("GET", "/api/brands"),
    ("POST", "/api/brands"),
    ("GET", "/api/team"),
    ("POST", "/api/team/invite"),
    ("POST", "/api/team/accept"),
}

# Routes this repo serves that the KNOWLEDGE BASE does not use. Each was
# checked against the console's own API layer (apps/web/app/api.ts) and its
# live views: either no function exists for it at all, or one exists and no
# view on the navigation calls it. The views that would have — review.tsx,
# taxonomy.tsx, library.tsx, search.tsx, collections.tsx — are orphaned: they
# compile and nothing imports them.
#
# Kept out of the SDK so a client library describes the product rather than
# the repository. If one of these becomes part of the KB, delete its line
# here and the test will demand the method.
NOT_IN_THE_KB = {
    ("GET", "/api/health"),
    ("GET", "/api/chunks/{chunk_id}/lineage"),
    ("GET", "/api/facets"),
    ("GET", "/api/review"),
    ("GET", "/api/snippets"),
    ("GET", "/api/items/{item_id}/related"),
    ("GET", "/api/items/{item_id}/pages/{page}/figures"),
    ("GET", "/api/classes"),
    ("POST", "/api/classes"),
    ("DELETE", "/api/classes/{class_id}"),
    ("GET", "/api/collections/{collection_id}/summaries/coverage"),
    ("GET", "/api/collections/{collection_id}/consolidation"),
    ("POST", "/api/collections/{collection_id}/consolidate"),
}

EXCUSED = BY_DESIGN | NOT_IN_THE_KB

_CLIENT = Path(__file__).resolve().parents[1] / "sdk" / "markvector" / "markvector" / "client.py"


def _normalise(path: str) -> str:
    """`/api/items/{item_id}` and `/api/items/{_doc_id(f)}` are the same route."""
    return re.sub(r"\{[^}]*\}", "{}", path)


def sdk_paths() -> set[str]:
    source = io.open(_CLIENT, encoding="utf-8").read()
    return {
        _normalise(m.group(1))
        for m in re.finditer(r'f?"(/api/[^"]*)"', source)
    }


def test_the_sdk_reaches_every_route_the_kb_uses():
    reachable = sdk_paths()
    missing = sorted(
        f"{method} {path}"
        for (method, path) in matrix()
        if (method, path) not in EXCUSED and _normalise(path) not in reachable
    )
    assert not missing, (
        "these routes accept an API key but no SDK method calls them:\n  "
        + "\n  ".join(missing)
    )


def test_the_exclusions_are_real_routes():
    """An allowlist that outlives the route it excused would quietly re-open
    the gap it was written to record."""
    known = set(matrix())
    assert EXCUSED <= known, sorted(EXCUSED - known)
