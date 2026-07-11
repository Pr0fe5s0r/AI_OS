from __future__ import annotations

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
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def ingest(source_config: dict, raw_payload: dict) -> Event:
    """Normalize a raw source payload into the unified Event schema."""
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
        content=str(m("content", "")).strip() or f"(no content) {m('id')}",
        metadata={k: _resolve(v, raw_payload, ctx) for k, v in mapping.get("metadata", {}).items()},
        raw=raw_payload,
    )
