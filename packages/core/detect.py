from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from packages.core.llm import chat
from packages.shared.schema import Evidence, NormBaseline, Situation

# Generic detection runtime: a declarative rule executor + optional LLM reasoning.
# It contains ZERO domain rules — every rule, threshold and prompt arrives as data
# from the calling vertical.
#
# rule = {
#   "name": str, "title": str, "severity": "low|medium|high|critical",
#   "select": {"source": ..., "type": ..., "where": [{field, op, value}, ...]},
#   "norm":  {"metric": ..., "k": 2.0},        # fire when age > median + k*std
#   "graph": {"node_type": ..., "missing_edge_type": ...},
#   "llm":   bool, "prompt": <key into state["prompts"]>, "limit": int
# }

_SEVERITY_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "situation",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "summary": {"type": "string"},
                "recommended_action": {"type": "string"},
            },
            "required": ["severity", "summary", "recommended_action"],
            "additionalProperties": False,
        },
    },
}


# ------------------------------- predicates -------------------------------


def _field(obj: dict, path: str) -> Any:
    cur: Any = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _as_names(value: Any) -> list[str]:
    out: list[str] = []
    for item in value or []:
        if isinstance(item, dict):
            out.append(str(item.get("name", "")).lower())
        else:
            out.append(str(item).lower())
    return out


def _match(event: dict, pred: dict, now: datetime) -> bool:
    value = _field(event, pred["field"])
    op = pred["op"]
    expected = pred.get("value")

    if op == "eq":
        return value == expected
    if op == "ne":
        return value != expected
    if op == "is_null":
        return value in (None, "", [], {})
    if op == "not_null":
        return value not in (None, "", [], {})
    if op == "contains_any":
        names = _as_names(value)
        return any(str(w).lower() in names for w in (expected or []))
    if op == "older_than_days":
        if not value or expected is None:
            return False
        ts = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (now - ts).total_seconds() / 86400.0 > float(str(expected))
    raise ValueError(f"unknown predicate op: {op!r}")


# ------------------------------- candidates -------------------------------


async def _event_candidates(session, company_id: str, rule: dict, now: datetime) -> list[dict]:
    sel = rule.get("select", {})
    clauses = ["company_id = :c"]
    params: dict[str, Any] = {"c": company_id}
    if sel.get("source"):
        clauses.append("source = :src")
        params["src"] = sel["source"]
    if sel.get("type"):
        clauses.append("type = :typ")
        params["typ"] = sel["type"]

    rows = await session.execute(
        text(
            f"""
            SELECT id, source, type, actor_name, timestamp, content, metadata
            FROM events WHERE {" AND ".join(clauses)}
            ORDER BY timestamp DESC LIMIT 500
            """
        ),
        params,
    )

    out: list[dict] = []
    for r in rows:
        md = r.metadata if isinstance(r.metadata, dict) else json.loads(r.metadata)
        ev = {
            "id": r.id,
            "source": r.source,
            "type": r.type,
            "actor_name": r.actor_name,
            "timestamp": r.timestamp,
            "content": r.content,
            "metadata": md or {},
        }
        if all(_match(ev, p, now) for p in sel.get("where", [])):
            out.append(ev)
    return out


async def _graph_candidates(session, company_id: str, rule: dict) -> list[dict]:
    """Nodes of a type that are missing an edge type — e.g. a PR closing nothing."""
    g = rule["graph"]
    rows = await session.execute(
        text(
            """
            SELECT n.id, n.type, n.label, n.source, n.metadata
            FROM nodes n
            WHERE n.company_id = :c AND n.type = :nt
              AND NOT EXISTS (
                SELECT 1 FROM edges e
                WHERE e.company_id = :c AND e.type = :et
                  AND (e.src_id = n.id OR e.dst_id = n.id)
              )
            LIMIT 200
            """
        ),
        {"c": company_id, "nt": g["node_type"], "et": g["missing_edge_type"]},
    )
    out: list[dict] = []
    for r in rows:
        md = r.metadata if isinstance(r.metadata, dict) else json.loads(r.metadata)
        md = md or {}
        ts = md.get("timestamp")
        out.append(
            {
                "id": r.id,
                "source": r.source or "graph",
                "type": r.type,
                "actor_name": md.get("actor_name", "unknown"),
                "timestamp": datetime.fromisoformat(ts) if ts else datetime.now(UTC),
                "content": md.get("content", r.label),
                "metadata": md,
            }
        )
    return out


def _norm_fires(event: dict, rule: dict, norms: list[NormBaseline], now: datetime) -> bool:
    spec = rule.get("norm")
    if not spec:
        return True
    baseline = next((n for n in norms if n.metric == spec["metric"]), None)
    if baseline is None or baseline.n == 0:
        return False
    age_hours = (now - event["timestamp"]).total_seconds() / 3600.0
    threshold = baseline.median + float(spec.get("k", 2.0)) * baseline.std
    return age_hours > threshold


# --------------------------------- detect ---------------------------------


def _evidence(event: dict) -> Evidence:
    return Evidence(
        event_id=event["id"],
        source=event["source"],
        timestamp=event["timestamp"],
        excerpt=(event["content"] or "")[:240].strip(),
        url=(event.get("metadata") or {}).get("url"),
    )


def _reason(rule: dict, prompts: dict, event: dict, norms: list[NormBaseline]) -> dict:
    template = prompts.get(rule.get("prompt", ""), "")
    if not template:
        return {}
    norm_txt = "; ".join(f"{n.metric}: median={n.median}{n.unit}, std={n.std}, n={n.n}" for n in norms)
    prompt = template.format(
        title=rule.get("title", rule["name"]),
        content=(event["content"] or "")[:1200],
        source=event["source"],
        actor=event["actor_name"],
        metadata=json.dumps(event.get("metadata", {}))[:600],
        norms=norm_txt or "none",
    )
    try:
        raw = chat([{"role": "user", "content": prompt}], response_format=_SEVERITY_SCHEMA)
        return json.loads(raw)
    except Exception:  # LLM/parse failure must not lose the rule hit
        return {}


async def detect(
    state: dict, detection_rules: list[dict], norms: list[NormBaseline]
) -> list[Situation]:
    """Run vertical-supplied rules against current state. Returns Situations.

    state = {"session": AsyncSession, "company_id": str, "prompts": dict}
    """
    session = state["session"]
    company_id = state["company_id"]
    prompts = state.get("prompts", {})
    now = datetime.now(UTC)

    situations: list[Situation] = []
    for rule in detection_rules:
        if rule.get("graph"):
            candidates = await _graph_candidates(session, company_id, rule)
        else:
            candidates = await _event_candidates(session, company_id, rule, now)

        candidates = [e for e in candidates if _norm_fires(e, rule, norms, now)]
        candidates = candidates[: int(rule.get("limit", 5))]

        for event in candidates:
            severity = rule.get("severity", "medium")
            summary = ""
            recommended = rule.get("recommended_action")

            if rule.get("llm"):
                verdict = _reason(rule, prompts, event, norms)
                severity = verdict.get("severity", severity)
                summary = verdict.get("summary", "")
                recommended = verdict.get("recommended_action", recommended)

            if not summary:
                summary = (event["content"] or "")[:200].strip()

            situations.append(
                Situation(
                    id=f"{rule['name']}:{event['id']}",
                    company_id=company_id,
                    rule=rule["name"],
                    severity=severity,
                    title=rule.get("title", rule["name"]),
                    summary=summary,
                    recommended_action=recommended,
                    evidence=[_evidence(event)],
                    status="open",
                    created_at=now,
                )
            )
    return situations
