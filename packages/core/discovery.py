from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from packages.core.ingest import ingest
from packages.core.llm import chat
from packages.core.profile import Profile

# Profile discovery (checkpoint 5): look at what a connector actually returns
# and PROPOSE a starter profile, so onboarding a company doesn't start with a
# blank YAML file.
#
# Two halves, deliberately split:
#   1. inspect_payloads() — pure, deterministic shape analysis. No LLM, no
#      network. This is what the golden test pins down.
#   2. propose_slots() — the LLM turns that inventory into profile slots.
#      Nondeterministic by nature, so everything it returns is VALIDATED
#      against real sample payloads before it's allowed to become a profile.
#
# Generic by construction: the heuristics below reason about the SHAPE of data
# (does this string parse as a date? is this field low-cardinality?), never
# about any industry. The same code proposes a profile for a GitHub repo, a
# Zendesk queue, or a warehouse feed.
#
# Scope note: an induced profile proposes sources/things/links/rhythms/
# vocabulary and deliberately leaves `moves` EMPTY. Moves are what the agent
# may DO to a customer's real systems — auto-inventing write actions from a
# payload shape would be reckless, so a human adds those. `watchers` is left
# empty too, and costs nothing: the engine's universal built-ins (CP2) fire on
# any company with no profile watchers at all, so an induced profile is
# useful the moment it's confirmed.

_ROLE_HINTS = {
    "identifier": ("id", "number", "key", "ref", "uid", "sku"),
    "actor": ("user", "author", "actor", "requester", "assignee", "owner", "creator"),
    "status": ("state", "status", "stage", "phase"),
    "title": ("title", "subject", "name", "summary", "headline"),
    "body": ("body", "description", "text", "content", "message", "notes"),
    "url": ("url", "link", "permalink", "html_url"),
    "labels": ("labels", "tags", "categories"),
}


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return "null"


def _is_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or len(value) < 8:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _flatten(raw: dict, prefix: str = "", depth: int = 0) -> dict[str, Any]:
    """Dotted paths -> values. Nested dicts are walked (shallowly — a payload's
    meaning lives near the surface); lists are summarized, not exploded."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and depth < 2:
            nested = _flatten(value, prefix=f"{path}.", depth=depth + 1)
            if nested:
                out.update(nested)
            else:
                out[path] = value
        else:
            out[path] = value
    return out


def _hinted_role(path: str) -> str | None:
    """A guess from the field's NAME. Exact leaf match first, so `id` doesn't
    lose to a fuzzy rule; then the two shapes an actor reliably takes —
    nested under an actor-ish object (`user.login`) or prefixed by one
    (`requester_id`)."""
    parts = path.lower().split(".")
    leaf = parts[-1]
    for role, hints in _ROLE_HINTS.items():
        if leaf in hints:
            return role
    if len(parts) > 1 and parts[-2] in _ROLE_HINTS["actor"]:
        return "actor"
    if any(leaf.startswith(f"{hint}_") for hint in _ROLE_HINTS["actor"]):
        return "actor"
    return None


def _classify(path: str, values: list[Any]) -> str:
    """The field's likely ROLE, from its values first and its name second.

    Values win: a field called `id` holding ISO dates is a timestamp. The name
    is only a tie-breaker, because names are a convention and values are fact.
    """
    real = [v for v in values if v not in (None, "", [], {})]
    if not real:
        return "empty"

    if all(_is_timestamp(v) for v in real):
        return "timestamp"

    types = {_type_name(v) for v in real}
    hint = _hinted_role(path)

    if types == {"string"} and all(str(v).startswith("http") for v in real):
        return "url"
    if types <= {"int", "float"}:
        return "identifier" if hint == "identifier" else "number"
    if types == {"list"}:
        return hint if hint == "labels" else "list"
    if types == {"dict"}:
        return hint if hint == "actor" else "object"
    if types == {"bool"}:
        return "flag"
    if types == {"string"}:
        distinct = {str(v) for v in real}
        longest = max(len(str(v)) for v in real)
        # A status is a short string that VARIES: 2..5 distinct values. The
        # "varies" part matters — a field with a single value across every
        # payload is a constant (a repo name, a tenant slug), not a state
        # machine, and treating it as one produces a nonsense status_field.
        if 2 <= len(distinct) <= 5 and longest <= 20:
            return "status" if hint is None or hint == "status" else hint
        if longest > 60:
            return "body" if hint is None or hint == "body" else hint
        return hint or "string"
    return hint or "mixed"


def inspect_payloads(raws: list[dict], max_samples: int = 2) -> dict[str, Any]:
    """Deterministic field inventory for a connector's raw payloads.

    Pure: no LLM, no network, no DB. Returns, per dotted field path, how often
    it's populated, what types it holds, a couple of real sample values, and a
    guessed ROLE — the evidence propose_slots() reasons over and the golden
    test asserts against.
    """
    total = len(raws)
    seen: dict[str, list[Any]] = {}
    for raw in raws:
        for path, value in _flatten(raw).items():
            seen.setdefault(path, []).append(value)

    fields: dict[str, Any] = {}
    for path, values in sorted(seen.items()):
        populated = [v for v in values if v not in (None, "", [], {})]
        fields[path] = {
            "role": _classify(path, values),
            "types": sorted({_type_name(v) for v in values}),
            "present": len(populated),
            "fill_rate": round(len(populated) / total, 2) if total else 0.0,
            "distinct": len({json.dumps(v, default=str) for v in populated}),
            "samples": [v for v in populated[:max_samples]],
        }
    return {"payloads": total, "fields": fields}


def _by_role(inventory: dict[str, Any], role: str) -> list[str]:
    return [p for p, f in inventory["fields"].items() if f["role"] == role]


def summarize_inventory(inventory: dict[str, Any]) -> str:
    """A compact, human/LLM-readable digest of inspect_payloads()."""
    lines = [f"{inventory['payloads']} sample payloads. Fields:"]
    for path, f in inventory["fields"].items():
        sample = json.dumps(f["samples"][0], default=str)[:80] if f["samples"] else "—"
        lines.append(
            f"- {path}: role={f['role']} types={'/'.join(f['types'])} "
            f"fill={f['fill_rate']} distinct={f['distinct']} e.g. {sample}"
        )
    return "\n".join(lines)


_SLOTS_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "induced_profile",
        "strict": False,
        "schema": {
            "type": "object",
            "properties": {
                "thing_type": {"type": "string", "description": "PascalCase name for the main record, e.g. Incident"},
                "event_type": {"type": "string", "description": "snake_case name for the event, e.g. issue"},
                "id_template": {"type": "string", "description": "unique id template using {field} placeholders"},
                "timestamp_field": {"type": "string"},
                "title_field": {"type": "string"},
                "body_field": {"type": "string"},
                "status_field": {"type": "string"},
                "actor_field": {"type": "string"},
                "url_field": {"type": "string"},
                "end_field": {"type": "string", "description": "field marking completion, for the resolution rhythm"},
                "metadata_fields": {"type": "array", "items": {"type": "string"}},
                "rhythm_name": {"type": "string", "description": "snake_case, e.g. issue_resolution_hours"},
                "vocabulary": {
                    "type": "object",
                    "properties": {
                        "thing": {"type": "string"},
                        "situation": {"type": "string"},
                        "actor": {"type": "string"},
                        "workspace": {"type": "string"},
                    },
                },
            },
            "required": ["thing_type", "event_type", "id_template", "timestamp_field", "title_field"],
        },
    },
}


def propose_slots(inventory: dict[str, Any], samples: list[dict], prompt_template: str) -> dict[str, Any]:
    """Ask the model to name and map what inspect_payloads() found.

    Returns the raw proposal dict. Never raises — a model/parse failure
    returns {} so the caller can fall back to the deterministic guess.
    """
    prompt = prompt_template.format(
        inventory=summarize_inventory(inventory),
        samples=json.dumps(samples[:2], default=str)[:3000],
    )
    try:
        raw = chat([{"role": "user", "content": prompt}], response_format=_SLOTS_SCHEMA)
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def deterministic_guess(inventory: dict[str, Any], source: str) -> dict[str, Any]:
    """A best-effort proposal from the inventory ALONE — no LLM.

    This is both the fallback when the model is unavailable and the floor the
    model's proposal is merged onto, so discovery degrades to something
    working rather than nothing.
    """
    fields = inventory["fields"]

    def _always_first(paths: list[str]) -> list[str]:
        """Prefer the field that is ALWAYS populated. Among several actors,
        the one present on every payload is the author; an assignee is often
        empty. Same logic picks the real title over an optional one."""
        return sorted(paths, key=lambda p: -fields[p]["fill_rate"])

    timestamps = _by_role(inventory, "timestamp")
    identifiers = _always_first(_by_role(inventory, "identifier"))
    titles = _always_first(_by_role(inventory, "title") or _by_role(inventory, "string"))
    bodies = _always_first(_by_role(inventory, "body"))
    statuses = _always_first(_by_role(inventory, "status"))
    urls = _always_first(_by_role(inventory, "url"))
    actors = _always_first(_by_role(inventory, "actor"))
    # the creation timestamp is the earliest-sounding one that's always present
    created = next((t for t in timestamps if fields[t]["fill_rate"] == 1.0), timestamps[0] if timestamps else "")
    # a completion field is a timestamp that is only SOMETIMES set — the
    # signature of "this finished" vs "this happened"
    end = next((t for t in timestamps if 0 < fields[t]["fill_rate"] < 1.0), "")

    return {
        "thing_type": "Record",
        "event_type": "record",
        "id_template": f"{source}-{{{identifiers[0]}}}" if identifiers else "",
        "timestamp_field": created,
        "title_field": titles[0] if titles else "",
        "body_field": bodies[0] if bodies else "",
        "status_field": statuses[0] if statuses else "",
        "actor_field": actors[0] if actors else "",
        "url_field": urls[0] if urls else "",
        "end_field": end,
        "metadata_fields": [p for p in (statuses + urls + identifiers + timestamps) if p],
        "rhythm_name": "record_resolution_hours",
        "vocabulary": {},
    }


def _mapping_from(proposal: dict[str, Any], source: str) -> dict[str, Any]:
    """Turn a proposal into the declarative mapping core.ingest() speaks."""
    actor = proposal.get("actor_field") or ""
    title = proposal.get("title_field") or ""
    body = proposal.get("body_field") or ""

    content: dict[str, Any]
    if title and body:
        content = {"concat": [{"path": title, "default": ""}, {"const": "\n\n"}, {"path": body, "default": ""}]}
    else:
        content = {"path": title or body, "default": ""}

    # actor is already a concrete path to a string (e.g. "user.login") — the
    # inventory resolves nested actor objects down to the name field itself
    mapping: dict[str, Any] = {
        "id": {"template": proposal.get("id_template") or f"{source}-{{id}}"},
        "type": {"const": proposal.get("event_type") or "record"},
        "actor_id": {"path": actor or "actor", "default": "unknown"},
        "actor_name": {"path": actor or "actor", "default": "unknown"},
        "timestamp": {"path": proposal.get("timestamp_field") or ""},
        "content": content,
        "metadata": {},
    }
    for path in proposal.get("metadata_fields") or []:
        mapping["metadata"][path.split(".")[-1]] = {"path": path}
    if proposal.get("url_field"):
        mapping["metadata"]["url"] = {"path": proposal["url_field"]}
    if proposal.get("end_field"):
        mapping["metadata"][proposal["end_field"].split(".")[-1]] = {"path": proposal["end_field"]}
    return mapping


def build_profile(company_id: str, source: str, connector: str, proposal: dict[str, Any]) -> Profile:
    """Assemble a Profile from a (merged) proposal. Pure — no IO."""
    thing_type = proposal.get("thing_type") or "Record"
    event_type = proposal.get("event_type") or "record"
    status_leaf = (proposal.get("status_field") or "").split(".")[-1]
    end_leaf = (proposal.get("end_field") or "").split(".")[-1]
    vocab = proposal.get("vocabulary") or {}

    rhythms = []
    if end_leaf:
        rhythms.append(
            {
                "name": proposal.get("rhythm_name") or f"{event_type}_resolution_hours",
                "unit": "hours",
                "window_days": 90,
                "source": source,
                "type": event_type,
                "end_field": end_leaf,
            }
        )

    return Profile(
        company_id=company_id,
        sources=[
            {
                "source": source,
                "kind": "connector",
                "connector": connector,
                "mapping": _mapping_from(proposal, source),
            }
        ],
        things={
            "types": [{"name": thing_type, "source": source, "event_type": event_type}],
            "default_type": "Event",
            "status_field": status_leaf or "status",
            "actor_thing": {"type": "Person"},
            "entity_rules": {
                "id_patterns": [r"#(\d+)", r"\b[A-Z]{3,}-\d+\b"],
                "similarity_hard": 0.68,
                "similarity_soft": 0.63,
                "candidate_limit": 25,
            },
        },
        links={
            "types": ["AUTHORED", "MENTIONS", "SAME_AS"],
            "same_as": "SAME_AS",
            "mentions": "MENTIONS",
            "authored": "AUTHORED",
        },
        rhythms=rhythms,
        # left empty on purpose — see the module docstring
        watchers=[],
        moves={},
        vocabulary={"terms": vocab} if vocab else {},
    )


def validate_profile(profile: Profile, source: str, raws: list[dict]) -> list[str]:
    """Prove the induced profile can normalize the very payloads it was
    induced from. A proposal that fails this is not a profile — it's a guess
    that would quietly produce garbage events.
    """
    errors: list[str] = []
    source_def = next((s for s in profile.sources if s["source"] == source), None)
    if source_def is None:
        return [f"no source {source!r} in the induced profile"]

    source_config = {
        "source": source,
        "company_id": profile.company_id,
        "context": {},
        "mapping": source_def["mapping"],
        "connector_type": source_def.get("connector"),
    }
    for i, raw in enumerate(raws[:5]):
        try:
            event = ingest(source_config, raw)
        except Exception as exc:
            errors.append(f"payload {i}: normalization raised {exc}")
            continue
        if not event.id or "None" in event.id or "{" in event.id:
            errors.append(f"payload {i}: unusable id {event.id!r}")
        if not event.content.strip() or event.content.startswith("(no content)"):
            errors.append(f"payload {i}: empty content")
        if event.actor.id == "unknown":
            errors.append(f"payload {i}: actor not resolved")
    return errors


def induce_profile(
    company_id: str,
    source: str,
    connector: str,
    raws: list[dict],
    prompt_template: str,
) -> tuple[Profile, dict[str, Any]]:
    """Propose a starter profile from real connector payloads.

    Returns ``(profile, report)``. The report carries the inventory, whether
    the model contributed, and any validation errors — an honest account of
    how the guess was reached, so a human confirming it can see the evidence
    rather than a black box.
    """
    if not raws:
        raise ValueError("induce_profile needs at least one real payload to look at")

    inventory = inspect_payloads(raws)
    floor = deterministic_guess(inventory, source)
    proposal = propose_slots(inventory, raws, prompt_template)
    # the model names things; the deterministic pass fills anything it skipped
    merged = {**floor, **{k: v for k, v in proposal.items() if v}}

    profile = build_profile(company_id, source, connector, merged)
    errors = validate_profile(profile, source, raws)

    if errors and proposal:
        # the model's naming broke normalization — fall back to the floor,
        # which is derived from the payloads themselves
        fallback = build_profile(company_id, source, connector, floor)
        fallback_errors = validate_profile(fallback, source, raws)
        if len(fallback_errors) < len(errors):
            profile, errors, merged = fallback, fallback_errors, floor

    return profile, {
        "inventory": inventory,
        "proposal": merged,
        "used_llm": bool(proposal),
        "errors": errors,
        "valid": not errors,
    }
