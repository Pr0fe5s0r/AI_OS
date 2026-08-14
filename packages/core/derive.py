from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.llm import chat
from packages.shared.schema import Scope

# ---------------------------------------------------------------------------
# DERIVED ATTRIBUTES — what a model can tell about a document, kept apart from
# what a caller asserted about it.
#
# The rule this module exists to enforce, stated once:
#
#   TRUSTED METADATA CONSTRAINS RETRIEVAL. DERIVED METADATA ONLY IMPROVES
#   DISCOVERY AND RANKING WITHIN THE SCOPE THAT CONSTRAINT ALREADY ALLOWED.
#
# That division is what makes a model's mistake survivable. A boost cannot add
# a document to a result set — it can only reorder one the trusted filter has
# already permitted — so a wrong attribute costs ranking quality and can never
# cost isolation. A filter built on the same guess could expose a document from
# the wrong client, which is why derived values must never reach `_filters()`
# or `_narrow_to_metadata()` in search.py. tests/test_derive.py fails if they
# ever do.
#
# Where the improvement actually lands is worth knowing, because it means there
# is no new ranking machinery here at all: derived attributes are written into
# the document CARD, and the card is already
#
#   * what routing.py scores documents on when deciding which file a question
#     is about, and
#   * excluded from retrieval evidence by chunks.keyword_search
#     (`node_type NOT IN ('card', 'section_summary')`).
#
# So an answer can be ROUTED by an inferred attribute and can never be CITED to
# one. The safety property is inherited from a rule that already existed rather
# than added by this file — which is the only reason it can be relied on.
# ---------------------------------------------------------------------------

# Keys a model may never write, at any confidence, even into its own namespace.
#
# Not because reading `derived.client_id` would be trusted today — nothing
# reads it as authority — but because these names are the ones a future bulk
# edit, migration or well-meaning "copy the derived values across" would move
# into the asserted column without anyone noticing the boundary being crossed.
# Refused at write time, where it is visible, rather than filtered at read time
# in every caller that ever touches this.
RESERVED_KEYS = frozenset(
    {
        "workspace_id",
        "collection_id",
        "company_id",
        "tenant_id",
        "client_id",
        "customer_id",
        "owner_id",
        "user_id",
        "account_id",
        "access_control",
        "acl",
        "permissions",
        "scopes",
        "visibility",
        "classification",
    }
)

# What the model is asked for. Deliberately small and closed: an open-ended
# "describe this document" produces a different vocabulary per document, and a
# facet nobody can enumerate is one nobody can filter or boost on.
_FIELDS = ("doc_type", "topics", "entities", "year")

MAX_TOPICS = 6
MAX_ENTITIES = 8
MAX_VALUE_CHARS = 60

_PROMPT = (
    "You are reading a summary of ONE document from a knowledge base. Return "
    "JSON describing it, for search and filtering. No prose, JSON only.\n\n"
    "{\n"
    '  "doc_type": one short lowercase noun phrase for what KIND of document '
    'this is — "invoice", "research paper", "policy", "statement of work", '
    '"user manual". Not what it is about.\n'
    '  "topics": up to 6 short lowercase subject tags.\n'
    '  "entities": up to 8 proper names actually named in the text — '
    "organisations, products, systems, places. Names only.\n"
    '  "year": the four-digit year the document is ABOUT, as a string, or null '
    "if it does not say.\n"
    "}\n\n"
    "Rules:\n"
    "- Only what the summary supports. Never guess, never infer from the "
    "filename, and never fill a field to avoid leaving it empty. An empty list "
    "is a correct answer and a wrong tag is not.\n"
    "- Do not output any field naming a client, customer, owner, tenant, "
    "account, permission or visibility. Those are not yours to decide.\n"
    "- Lowercase everything except entity names.\n\n"
    "SUMMARY:\n"
)

_JSON = re.compile(r"\{.*\}", re.DOTALL)


def _clean_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    text_value = " ".join(str(value).split())[:MAX_VALUE_CHARS].strip()
    return text_value or None


def _clean_list(value: Any, cap: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for entry in value:
        cleaned = _clean_value(entry)
        if cleaned and cleaned not in out:
            out.append(cleaned)
        if len(out) >= cap:
            break
    return out


def sanitise(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only the closed field set, cleaned, with reserved keys refused.

    Refusal is silent and total: a model that emits `client_id` has that key
    dropped, not renamed or nested. There is no path by which a value it
    invented becomes something the store treats as asserted.
    """
    out: dict[str, Any] = {}
    doc_type = _clean_value(raw.get("doc_type"))
    if doc_type:
        out["doc_type"] = doc_type.lower()
    topics = [topic.lower() for topic in _clean_list(raw.get("topics"), MAX_TOPICS)]
    if topics:
        out["topics"] = topics
    entities = _clean_list(raw.get("entities"), MAX_ENTITIES)
    if entities:
        out["entities"] = entities
    year = _clean_value(raw.get("year"))
    if year and re.fullmatch(r"(1[89]|20)\d{2}", year):
        out["year"] = year
    # Belt and braces. Nothing above can produce a reserved key, and this still
    # runs — the cost is a set lookup and the alternative is trusting that no
    # future field ever collides.
    return {key: value for key, value in out.items() if key not in RESERVED_KEYS}


async def describe(card: str, *, model: str | None = None) -> dict[str, Any]:
    """Ask a model what this document is, from the card already written for it.

    Reads the CARD rather than the document: it is two or three sentences built
    from every section summary, so it costs one small call instead of one per
    page, and it is the same text routing already ranks on — which keeps what
    is extracted aligned with what it will improve.

    Never raises. A failed extraction leaves a document with no derived
    attributes, which is exactly the state every document is in today; a store
    that could not ingest because a labelling call timed out would be a worse
    product than one that labels nothing.
    """
    if not card.strip():
        return {}
    try:
        reply = await _ask(card, model)
    except Exception:
        return {}
    match = _JSON.search(reply or "")
    if not match:
        return {}
    try:
        raw = json.loads(match.group(0))
    except (ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return sanitise(raw)


async def _ask(card: str, model: str | None) -> str:
    import asyncio

    return await asyncio.to_thread(
        chat,
        [{"role": "user", "content": _PROMPT + card}],
        temperature=0.0,
        model=model,
    )


def as_card_line(derived: dict[str, Any]) -> str:
    """The derived attributes as one line to append to a document's card.

    This is the entire "boost" in stage one, and it needs no scoring code:
    routing already ranks documents by card match, and the card is already
    embedded and searchable, so an inferred entity or topic starts helping the
    moment it is written into the text. Nothing new can rank, and nothing new
    can be cited — the card was already barred from evidence.

    Rendered plainly rather than as JSON: this text is embedded and matched
    against a question, and "doc_type: statement of work" reads to an embedding
    model roughly as a question about statements of work does. Braces do not.
    """
    if not derived:
        return ""
    parts: list[str] = []
    if derived.get("doc_type"):
        parts.append(f"Document type: {derived['doc_type']}.")
    if derived.get("topics"):
        parts.append(f"Topics: {', '.join(derived['topics'])}.")
    if derived.get("entities"):
        parts.append(f"Mentions: {', '.join(derived['entities'])}.")
    if derived.get("year"):
        parts.append(f"Year: {derived['year']}.")
    return " ".join(parts)


async def store(
    session: AsyncSession, scope: Scope, item_id: str, derived: dict[str, Any]
) -> None:
    """Write derived attributes to their own column, for the active version.

    Written whole rather than merged: a re-run reflects what the model says
    now, and a key that has stopped being true should disappear rather than
    linger because nothing overwrote it.
    """
    await session.execute(
        text(
            "UPDATE kb_items SET derived = CAST(:derived AS jsonb) "
            "WHERE workspace_id = :workspace AND item_id = :item AND status = 'active'"
        ),
        {
            "derived": json.dumps(sanitise(derived)),
            "workspace": scope.workspace_id,
            "item": item_id,
        },
    )


__all__ = [
    "MAX_ENTITIES",
    "MAX_TOPICS",
    "RESERVED_KEYS",
    "as_card_line",
    "describe",
    "sanitise",
    "store",
]
