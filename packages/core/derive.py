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

# The same idea one word at a time, so a name nobody thought to list still
# fails: `client_name`, `owner_email`, `account_reference`, `tenant_slug`.
_RESERVED_WORDS = frozenset(
    {
        "acl",
        "access",
        "account",
        "client",
        "customer",
        "owner",
        "permission",
        "permissions",
        "scope",
        "scopes",
        "tenant",
        "visibility",
        "workspace",
    }
)

# The vocabulary is OPEN. The model names whatever characterises the document —
# type, topics, subjects, entities, purpose, audience, period, jurisdiction,
# whatever a reader would actually use to recognise it.
#
# A closed field set was tried first and it was the wrong instinct for this
# mechanism. A fixed enum helps FILTERING, where two documents must agree on a
# label to be selected together. Here the attributes are rendered into the
# document's card, and the card is embedded — so a specific, document-shaped
# phrase ("statement of work for a casino loyalty platform") carries more signal
# to an embedding than a generic bucket ("specification") ever could. Forcing a
# vocabulary throws away exactly the part that helps.
#
# What is NOT open is the shape: flat keys, scalar or list-of-scalar values,
# bounded in every direction, with reserved names refused. Open vocabulary is
# not open season on the record.
MAX_KEYS = 12
MAX_LIST = 8
MAX_VALUE_CHARS = 80
MAX_KEY_CHARS = 32
# Everything rendered into the card, together. A card is a NAVIGATION surface a
# model reads dozens of at a time; attributes that ran to a page would drown the
# summary they are meant to sharpen.
MAX_CARD_LINE_CHARS = 700

_PROMPT = (
    "You are reading a summary of ONE document from a knowledge base. Describe "
    "it so that someone searching later can FIND it. Reply with JSON only, no "
    "prose.\n\n"
    "Choose the fields yourself — whatever actually characterises THIS "
    "document. There is no fixed schema, and two documents should not be "
    "described alike unless they are alike. Useful things to consider, none of "
    "them required: what kind of document it is, what it is about, its "
    "purpose, who it is for, the subjects and concepts it covers, the systems, "
    "organisations, products, people or places it names, the period it "
    "concerns, the jurisdiction or standard it belongs to.\n\n"
    "Shape:\n"
    '- A flat JSON object: {"field": "value"} or {"field": ["a", "b"]}.\n'
    "- Lowercase snake_case field names. No nesting, no objects inside values.\n"
    "- Short values. A phrase, not a sentence.\n\n"
    "Rules:\n"
    "- Only what the summary supports. Never guess, never infer from a "
    "filename, and never add a field to avoid leaving the object small. Four "
    "true fields beat ten padded ones, and an empty object is a correct answer "
    "for a document that says nothing definite.\n"
    "- Be specific. 'topics: networking' is worth little; 'topics: [routing "
    "algorithms, congestion control, the data link layer]' is worth a lot.\n"
    "- Never output a field naming a client, customer, owner, tenant, account, "
    "workspace, permission, visibility or access control. Those are not yours "
    "to decide, and they will be discarded.\n\n"
    "SUMMARY:\n"
)

_JSON = re.compile(r"\{.*\}", re.DOTALL)

_STRUCTURAL = frozenset("{}[]:,")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")


def _repair_json_like(blob: str) -> str:
    """Rewrite a JavaScript-ish object literal as strict JSON.

    A SCANNER rather than substitutions, and that is the whole point of it.
    Regexes cannot tell whether a comma is separating two list items or sitting
    inside a quoted string, and the version that tried turned

        "setting": "wizarding world, primarily hogwarts"

    into `"wizarding world, "primarily hogwarts""` — corrupting a value that had
    been perfectly well formed to begin with, and losing the whole document's
    attributes to a parse error. Walking the text means quoted strings are
    copied out verbatim and only what is genuinely outside them is repaired.

    Handles every shape this provider has actually produced: unquoted keys,
    unquoted values, and entries with no commas between them at all.
    """
    out: list[str] = []
    index, length = 0, len(blob)
    complete = False  # a whole value was just emitted, so the next one needs a comma

    while index < length:
        char = blob[index]
        if char.isspace():
            index += 1
            continue
        if char in "}]":
            out.append(char)
            index += 1
            complete = True
            continue
        if char in ":,":
            out.append(char)
            index += 1
            complete = False
            continue
        if complete:
            out.append(",")
            complete = False
        if char in "{[":
            out.append(char)
            index += 1
            continue
        if char == '"':
            end = index + 1
            while end < length:
                if blob[end] == "\\":
                    end += 2
                    continue
                if blob[end] == '"':
                    break
                end += 1
            out.append(blob[index : end + 1])
            index = end + 1
            complete = True
            continue

        end = index
        while end < length and blob[end] not in _STRUCTURAL and blob[end] != "\n":
            end += 1
        token = blob[index:end].strip()
        index = end
        if not token:
            continue
        lowered = token.lower()
        if lowered in ("true", "false", "null") or _NUMBER.fullmatch(token):
            out.append(lowered if lowered in ("true", "false", "null") else token)
        else:
            out.append(json.dumps(token))
        complete = True

    return "".join(out)


def _loads_lenient(blob: str) -> Any:
    """Parse JSON, then parse what a model actually sent.

    Measured, and not an edge case: asked for JSON with an open set of fields,
    this provider replies with JavaScript object literals — unquoted keys,
    sometimes unquoted values, sometimes no commas at all:

        { document_type: technical overview
          subject: bitcoin
          topics: [peer-to-peer electronic cash, merkle trees] }

    `json.loads` rejects all of that, and three of eight real documents were
    silently derived as `{}` because of it. The repair runs only after a strict
    parse has already failed, so a well-formed reply is never touched by it.

    Returns None for anything that is not an OBJECT. The repair is deliberately
    forgiving, and forgiving enough that a line of prose becomes a valid JSON
    string — which parses, and is not a description of a document. Only the one
    shape this asked for counts as an answer.
    """
    for candidate in (blob, _repair_json_like(blob)):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _clean_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    text_value = " ".join(str(value).split())[:MAX_VALUE_CHARS].strip()
    return text_value or None


def _clean_list(value: Any, cap: int) -> list[str]:
    out: list[str] = []
    for entry in value:
        cleaned = _clean_value(entry)
        if cleaned and cleaned not in out:
            out.append(cleaned)
        if len(out) >= cap:
            break
    return out


_KEY_JUNK = re.compile(r"[^a-z0-9_]+")


def _clean_key(key: Any) -> str | None:
    """Normalise a field name the model chose, or reject it.

    Normalisation happens BEFORE the reserved check, which is the whole reason
    it exists here rather than in a caller: "Client ID" and "client-id" must
    both become `client_id` and be refused, not slip through because they were
    spelled differently from the list.
    """
    if not isinstance(key, str):
        return None
    name = _KEY_JUNK.sub("_", key.strip().lower()).strip("_")[:MAX_KEY_CHARS]
    if not name or not name[0].isalpha():
        return None
    return name


def _is_reserved(name: str) -> bool:
    """True for anything naming ownership, tenancy or access.

    Checked per word, not per whole name, so `client_id` and `owner_email` and
    `account_reference` all fail rather than only the exact spellings anyone
    thought to list.

    This will occasionally refuse an honest descriptive field — a networking
    textbook might reasonably want `access_control`, since that is a real
    chapter of one. That is the trade taken deliberately: refusing a field
    costs a small ranking nudge, while allowing one named `access_control` to
    exist in a store where a future migration might copy derived values into
    asserted ones costs the boundary this whole design is built on.
    """
    if name in RESERVED_KEYS:
        return True
    return bool(_RESERVED_WORDS & set(name.split("_")))


def sanitise(raw: dict[str, Any]) -> dict[str, Any]:
    """Open vocabulary, bounded shape, reserved names refused.

    The model names its own fields, because a description that fits the
    document is worth more here than one that fits a schema. What it may not do
    is change the SHAPE of the record: flat keys, scalar or list-of-scalar
    values, everything capped, and nothing that names ownership or access.

    Refusal is silent and total. A model that emits `client_id` has that key
    dropped — not renamed, not nested, not kept under a warning — so there is
    no path by which a value it invented becomes something the store treats as
    asserted.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if len(out) >= MAX_KEYS:
            break
        name = _clean_key(key)
        if name is None or _is_reserved(name):
            continue
        if isinstance(value, list):
            cleaned_list = _clean_list(value, MAX_LIST)
            if cleaned_list:
                out[name] = cleaned_list
        else:
            cleaned = _clean_value(value)
            if cleaned:
                out[name] = cleaned
    return out


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
    # Tried twice, because the failure is not deterministic. Across two runs of
    # the same eight documents, three came back unusable the first time and a
    # different one the second — a document that describes itself fine on one
    # attempt and not on the next. One retry is a cheap fix for a document
    # otherwise left undescribed until somebody re-ingests it.
    for _attempt in range(2):
        try:
            reply = await _ask(card, model)
        except Exception:
            # Retried, not abandoned. This said `return {}` first, which gave up
            # on exactly the failure a retry exists for: a backfill firing one
            # call per document in quick succession meets a rate limit or a
            # timeout, and four of eight documents came back undescribed while
            # the model was answering every one of them correctly when asked
            # again a minute later.
            continue
        match = _JSON.search(reply or "")
        if not match:
            continue
        raw = _loads_lenient(match.group(0))
        if isinstance(raw, dict):
            cleaned = sanitise(raw)
            if cleaned:
                return cleaned
    return {}


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
    for name, value in derived.items():
        label = name.replace("_", " ").strip().capitalize()
        said = ", ".join(value) if isinstance(value, list) else str(value)
        if said:
            parts.append(f"{label}: {said}.")
    line = " ".join(parts)
    if len(line) <= MAX_CARD_LINE_CHARS:
        return line
    # Trimmed at a field boundary rather than mid-phrase: half a truncated
    # entity name is noise in an embedding, and the fields are already in the
    # order the model thought most characteristic.
    kept: list[str] = []
    used = 0
    for part in parts:
        if used + len(part) + 1 > MAX_CARD_LINE_CHARS:
            break
        kept.append(part)
        used += len(part) + 1
    return " ".join(kept)


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
    "MAX_KEYS",
    "MAX_LIST",
    "RESERVED_KEYS",
    "as_card_line",
    "describe",
    "sanitise",
    "store",
]
