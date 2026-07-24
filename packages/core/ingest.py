from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from packages.shared.schema import Actor, Event

# Generic event normalizer. The vertical's connector_config supplies a
# declarative `mapping` (raw field -> event field); the core applies it with no
# knowledge of GitHub/Slack/etc. Supported mapping ops:
#   {"path": "a.b.c", "default": ...}   dotted lookup into the raw payload
#   {"const": value}                     literal
#   {"concat": [op, op, ...]}            string-join resolved parts
#   {"template": "gh-{repo}-{number}"}   .format() over context + top-level raw
#   {"when_exists": "path", "then": op, "else": op}   conditional on presence
#   <literal>                            used as-is


def _dig(raw: dict, path: str, default: Any = None) -> Any:
    cur: Any = raw
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def _resolve(spec: Any, raw: dict, ctx: dict) -> Any:
    if not isinstance(spec, dict):
        return spec
    if "path" in spec:
        return _dig(raw, spec["path"], spec.get("default"))
    if "const" in spec:
        return spec["const"]
    if "concat" in spec:
        return "".join(str(_resolve(s, raw, ctx) or "") for s in spec["concat"])
    if "template" in spec:
        flat = {k: v for k, v in raw.items() if isinstance(v, str | int | float)}
        return spec["template"].format(**{**flat, **ctx})
    if "epoch" in spec:
        value = _dig(raw, spec["epoch"])
        return datetime.fromtimestamp(float(value), UTC) if value else None
    if "when_exists" in spec:
        present = _dig(raw, spec["when_exists"], None) is not None
        return _resolve(spec["then"] if present else spec.get("else"), raw, ctx)
    return spec


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return datetime.now(UTC)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value).strip()
    # Unix epoch, as many APIs emit it — Slack's `ts` is "seconds.micros"
    # ("1784921347.508139"), which is NOT ISO and used to blow up ingest. A
    # bare numeric (optionally with a fractional part) is an epoch, not a date.
    if re.fullmatch(r"\d{9,}(\.\d+)?", text):
        return datetime.fromtimestamp(float(text), tz=UTC)
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _tidy(content: str) -> str:
    """Collapse the holes a `concat` leaves when one of its parts is empty.

    Every stanza joins with a blank line, so a record missing one of them — a
    pull request with no description — came out with a four-line gap in the
    middle. It looks like a rendering fault on screen, and the padding is dead
    weight in the embedding. Fixed here rather than in any one mapping: this is
    a property of joining optional parts, not of GitHub.
    """
    return re.sub(r"\n{3,}", "\n\n", content).strip()


def apply_declared_mapping(source: str, mapping: dict[str, Any]) -> dict[str, Any]:
    """Fold a connector's DECLARED mapping additions into an induced mapping.

    Discovery induces a mapping once, from the payloads it saw that day, and
    the result is frozen into a profile version. But some fields are not
    induced at all — the connector states them, the way it states its moves
    and its target pattern. When a connector learns to report something new
    (which files a pull request touched), every existing profile would
    otherwise stay blind to it until somebody re-ran discovery, on a workspace
    that had no reason to think anything was missing.

    So this is applied on READ, exactly like with_connector_moves: induced
    knowledge stays versioned and auditable, declared knowledge refreshes with
    the code that declares it. Idempotent — folding twice must not append the
    same stanza twice, because a loaded profile can be saved and loaded again.
    """
    from packages.connectors.base import (
        actor_name_field_for,
        content_extra_fields_for,
        extra_metadata_fields_for,
    )

    mapping = dict(mapping)
    # The readable author. Some sources carry only an opaque id in the payload
    # (Slack's `user` = "U0…") and the connector resolves the real name into an
    # enrichment field. Declared, not induced, because only the connector knows
    # it added it — so an existing profile gets readable names on the next read,
    # no reseed, exactly like the moves/metadata overlays above.
    if actor_field := actor_name_field_for(source):
        mapping["actor_name"] = {"path": actor_field, "default": "unknown"}
    for extra in content_extra_fields_for(source):
        content = mapping.get("content")
        parts = list(content["concat"]) if isinstance(content, dict) and "concat" in content else (
            [content] if content is not None else []
        )
        if not any(isinstance(p, dict) and p.get("path") == extra for p in parts):
            parts += [{"const": "\n\n"}, {"path": extra, "default": ""}]
            mapping["content"] = {"concat": parts} if len(parts) > 1 else parts[0]

    declared = extra_metadata_fields_for(source)
    if declared:
        metadata = dict(mapping.get("metadata") or {})
        for path in declared:
            metadata.setdefault(path.split(".")[-1], {"path": path})
        mapping["metadata"] = metadata
    return mapping


def ingest(source_config: dict, raw_payload: dict, backfilled: bool = False) -> Event:
    """Normalize a raw source payload into the unified Event schema.

    ``backfilled`` is plumbing, not profile data: it says how THIS call was
    invoked (a history walk vs. a live sync/webhook), never something a
    profile mapping could know about its own raw payload.
    """
    mapping = source_config["mapping"]
    ctx = source_config.get("context", {})

    def m(field: str, default: Any = None) -> Any:
        if field not in mapping:
            return default
        return _resolve(mapping[field], raw_payload, ctx)

    return Event(
        id=str(m("id")),
        company_id=source_config.get("company_id", "default"),
        source=source_config["source"],
        type=str(m("type", "event")),
        actor=Actor(
            id=str(m("actor_id", "unknown")),
            name=str(m("actor_name", "unknown")),
            email=m("actor_email"),
        ),
        timestamp=_parse_ts(m("timestamp")),
        content=_tidy(str(m("content", ""))) or f"(no content) {m('id')}",
        metadata={k: _resolve(v, raw_payload, ctx) for k, v in mapping.get("metadata", {}).items()},
        raw=raw_payload,
        backfilled=backfilled,
    )
