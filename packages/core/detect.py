from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from packages.core import graph
from packages.core.llm import chat
from packages.shared.schema import Evidence, NormBaseline, Situation

if TYPE_CHECKING:
    from packages.core.profile import Profile

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
    if op in ("lt", "gt"):
        if value is None or expected is None:
            return False
        try:
            left, right = float(value), float(str(expected))
        except (TypeError, ValueError):
            return False
        return left < right if op == "lt" else left > right
    if op == "older_than_days":
        if not value or expected is None:
            return False
        ts = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (now - ts).total_seconds() / 86400.0 > float(str(expected))
    raise ValueError(f"unknown predicate op: {op!r}")


# ------------------------------- candidates -------------------------------


async def _event_candidates(session, company_id: str, rule: dict, now: datetime) -> list[dict]:
    sel = rule.get("select", {})
    # backfilled=false: the watcher engine reacts to LIVE state only. History
    # walked in by checkpoint 2's backfill exists to give norms real depth,
    # not to retroactively raise alerts on events from months ago.
    clauses = ["company_id = :c", "backfilled = false"]
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
    """Things missing a link type — e.g. a PR closing nothing, a PO with no
    delivery. The graph query runs in core.graph (Cypher); event content is
    hydrated from Postgres, the system of record."""
    g = rule["graph"]
    things = await graph.things_missing_link(
        company_id,
        thing_type=g["thing_type"],
        rel_type=g["missing_link_type"],
    )
    if not things:
        return []
    by_id = {t["id"]: t for t in things}
    rows = await session.execute(
        text(
            """
            SELECT id, source, type, actor_name, timestamp, content, metadata
            FROM events WHERE company_id = :c AND id = ANY(:ids)
            """
        ),
        {"c": company_id, "ids": list(by_id)},
    )
    out: list[dict] = []
    for r in rows:
        md = r.metadata if isinstance(r.metadata, dict) else json.loads(r.metadata)
        out.append(
            {
                "id": r.id,
                "source": r.source,
                "type": r.type,
                "actor_name": r.actor_name,
                "timestamp": r.timestamp,
                "content": r.content,
                "metadata": md or {},
            }
        )
    # a Thing with no Postgres event (e.g. an actor) still fires, minimally
    for tid, t in by_id.items():
        if not any(o["id"] == tid for o in out):
            ts = t.get("last_activity")
            out.append(
                {
                    "id": tid,
                    "source": "graph",
                    "type": t.get("thing_type", "Thing"),
                    "actor_name": "unknown",
                    "timestamp": datetime.fromisoformat(ts) if ts else datetime.now(UTC),
                    "content": t.get("title") or tid,
                    "metadata": {"status": t.get("status")},
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


_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _dedupe_by_entity(situations: list[Situation]) -> list[Situation]:
    """Multiple watchers can legitimately match the same underlying event
    (e.g. an issue is both unassigned AND past its SLA) — but surfacing one
    card per watcher for the same entity reads as duplicates in the feed.
    Keep only the highest-severity situation per entity."""
    best: dict[str, Situation] = {}
    for s in situations:
        key = s.evidence[0].event_id if s.evidence else s.id
        current = best.get(key)
        if current is None or _SEVERITY_RANK.get(s.severity, 3) < _SEVERITY_RANK.get(
            current.severity, 3
        ):
            best[key] = s
    return list(best.values())


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
    return _dedupe_by_entity(situations)


async def run_watcher_engine(state: dict, profile: Profile, norms: list[NormBaseline]) -> list[Situation]:
    """Checkpoint 2, part C: the watcher engine proper. Evaluates TWO
    origins — universal built-in primitives (no watcher declaration needed
    anywhere) and profile-defined Tier-1 watchers (`detect`, above) — and
    collapses both through the SAME highest-severity-wins dedupe, so a real
    issue that's both e.g. an orphaned hotspot AND past its SLA shows once.

    state = {"session": AsyncSession, "company_id": str, "prompts": dict}
    """
    from packages.core import watchers as universal

    session = state["session"]
    company_id = state["company_id"]

    situations = await detect(state, profile.watchers, norms)

    sources = [
        s["source"] for s in profile.sources
        if s.get("kind") == "connector" and s.get("enabled", True)
    ]
    situations += await universal.stalled_things(session, company_id)
    situations += await universal.aging_commitments(session, company_id)
    situations += await universal.orphaned_hotspots(session, company_id)
    situations += await universal.broken_rhythms(session, company_id, profile.rhythms)
    situations += await universal.volume_anomalies(session, company_id, sources)

    return _dedupe_by_entity(situations)
