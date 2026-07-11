from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.situations import list_situations

# Generic workspace briefing: gather the state, then evaluate a vertical-supplied
# policy over it. The core holds NO opinion about what "needs attention" means —
# the policy is an ordered list of declarative rules, exactly like detection_rules:
#
#   {"mode": "needs_human",
#    "when": [{"field": "pending_approvals", "op": "gt", "value": 0}],
#    "headline": "{pending_approvals} action{pending_approvals_s} waiting for approval.",
#    "next_best_action": "Open the command center and approve or reject."}
#
# First rule whose conditions all hold wins. A rule with no conditions is the
# default and must come last.

_OPS: dict[str, Any] = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
}


async def summarize_state(session: AsyncSession, company_id: str) -> dict:
    """Raw, opinion-free counts across every layer, company-scoped."""
    event_rows = await session.execute(
        text(
            """
            SELECT source, type, count(*) AS count, max(timestamp) AS latest
            FROM events WHERE company_id = :c
            GROUP BY source, type ORDER BY count(*) DESC
            """
        ),
        {"c": company_id},
    )
    situation_rows = await session.execute(
        text(
            "SELECT severity, status, count(*) AS count FROM situations "
            "WHERE company_id = :c GROUP BY severity, status"
        ),
        {"c": company_id},
    )
    action_rows = await session.execute(
        text("SELECT status, count(*) AS count FROM actions WHERE company_id = :c GROUP BY status"),
        {"c": company_id},
    )
    norm_rows = await session.execute(
        text(
            "SELECT metric, n, median, unit FROM norm_baselines "
            "WHERE company_id = :c ORDER BY metric"
        ),
        {"c": company_id},
    )
    sources_connected = int(
        (
            await session.execute(
                text("SELECT count(*) FROM credentials WHERE company_id = :c"), {"c": company_id}
            )
        ).scalar_one()
    )
    recent = await list_situations(session, company_id, limit=10)

    events = [
        {"source": r.source, "type": r.type, "count": r.count,
         "latest": r.latest.isoformat() if r.latest else None}
        for r in event_rows
    ]
    situations = [{"severity": r.severity, "status": r.status, "count": r.count} for r in situation_rows]
    actions = [{"status": r.status, "count": r.count} for r in action_rows]
    norms = [{"metric": r.metric, "n": r.n, "median": float(r.median), "unit": r.unit} for r in norm_rows]

    active = [s for s in situations if s["status"] != "resolved"]
    stats = {
        "events": sum(e["count"] for e in events),
        "active_situations": sum(s["count"] for s in active),
        "high_risk": sum(s["count"] for s in active if s["severity"] in ("critical", "high")),
        "pending_approvals": sum(a["count"] for a in actions if a["status"] == "pending_approval"),
        # a metric with zero observations has learned NOTHING — do not count it
        "learned_norms": sum(1 for n in norms if n["n"] > 0),
        "norm_metrics": len(norms),
        "sources_connected": sources_connected,
    }

    watchlist = [
        {"id": s.id, "severity": s.severity, "title": s.title, "summary": s.summary,
         "recommended_action": s.recommended_action, "status": s.status,
         "evidence_count": len(s.evidence)}
        for s in recent
        if s.status != "resolved"
    ][:3]

    return {
        "company_id": company_id,
        "stats": stats,
        "events": events,
        "situations": situations,
        "actions": actions,
        "norms": norms,
        "watchlist": watchlist,
    }


def evaluate_policy(state: dict, policy: list[dict]) -> dict:
    """First matching rule wins. Headlines are templated from the stats."""
    stats = state["stats"]
    # `{pending_approvals_s}` -> "" or "s", so verticals can pluralize in templates
    fmt: dict[str, Any] = dict(stats)
    fmt.update({f"{k}_s": ("" if v == 1 else "s") for k, v in stats.items() if isinstance(v, int)})

    for rule in policy:
        conditions = rule.get("when", [])
        if all(_OPS[c["op"]](stats.get(c["field"], 0), c["value"]) for c in conditions):
            return {
                "mode": rule["mode"],
                "headline": rule["headline"].format(**fmt),
                "next_best_action": rule["next_best_action"].format(**fmt),
            }
    return {"mode": "unknown", "headline": "", "next_best_action": ""}


async def build_briefing(session: AsyncSession, company_id: str, policy: list[dict]) -> dict:
    state = await summarize_state(session, company_id)
    return {**state, **evaluate_policy(state, policy)}
