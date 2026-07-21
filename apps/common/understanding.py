from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apps.common.context import DRY_RUN_KEY, approval_policy
from packages.core import connector_health as ch
from packages.core import watchers
from packages.core.credentials import list_connections
from packages.core.items import item_facets
from packages.core.norms import get_norms
from packages.core.profile import Profile
from packages.core.settings import get_setting

# "What do you actually know about my company?"
#
# Everything here already existed — the profile, the learned baselines, the
# connector health, the safety switch — but only as separate API calls a
# person never makes. Two thirds of the system was invisible, which is why it
# felt unknowable. This assembles the whole picture in one answer.
#
# It returns FACTS, not sentences. The wording belongs to the UI (and to the
# company's own vocabulary); putting English in here would bake one language
# and one tone into the engine's orchestration layer.


def _lifecycle(profile: Profile) -> list[dict[str, Any]]:
    """What "finished" means for each kind of work — the single most useful
    thing the system learns, and the one nobody could see."""
    out = []
    for rhythm in profile.rhythms:
        out.append(
            {
                "metric": rhythm.get("name"),
                "source": rhythm.get("source"),
                "type": rhythm.get("type"),
                "finished_when": rhythm.get("end_field"),
                "unit": rhythm.get("unit", "hours"),
            }
        )
    return out


def _allowed_actions(profile: Profile) -> list[dict[str, Any]]:
    registry = profile.moves.get("registry", {}) or {}
    autonomy = profile.moves.get("autonomy", {}) or {}
    allowed = set(autonomy.get("allowed_actions") or registry.keys())
    return [
        {
            "name": name,
            "external_effect": entry.get("kind") == "http",
            "approval_required": bool(entry.get("approval_required", True)),
            "autonomous": name in allowed,
        }
        for name, entry in sorted(registry.items())
    ]


async def describe(session: AsyncSession, profile: Profile) -> dict[str, Any]:
    """The whole picture: what we watch, what we learned, what we check for,
    what we may do, and whether we are allowed to touch anything for real."""
    company_id = profile.company_id
    status_field = (profile.things or {}).get("status_field")

    connections = await list_connections(session, company_id)
    health = {h["connector_type"]: h for h in await ch.get_health(session, company_id)}
    facets = await item_facets(session, company_id, status_field=status_field)
    norms = await get_norms(session, company_id, scope="business")
    policy = await approval_policy(session, profile)
    practice = await get_setting(session, company_id, DRY_RUN_KEY, policy.get("dry_run", True))

    # every connector the profile declares, whether or not it's connected yet
    declared = [s for s in profile.sources if s.get("kind") == "connector"]
    connected = {c.source: c for c in connections}
    counts = {f["key"]: f["count"] for f in facets["sources"]}

    watching = []
    for source_def in declared:
        source = source_def["source"]
        conn = connected.get(source)
        h = health.get(source, {})
        # the one config value that says WHAT is being watched (repo/channel/…)
        config = (conn.config if conn else {}) or {}
        target = next((str(v) for k, v in config.items() if k != "limit"), None)
        watching.append(
            {
                "source": source,
                "connected": conn is not None,
                "enabled": source_def.get("enabled", True),
                "target": target,
                "records": counts.get(source, 0),
                "health": h.get("status"),
                "last_success_at": h.get("last_success_at"),
                "consecutive_failures": h.get("consecutive_failures", 0),
            }
        )

    return {
        "company_id": company_id,
        "terms": profile.vocabulary.get("terms", {}) or {},
        "profile_version": profile.version,
        "watching": watching,
        "records": {
            "total": sum(counts.values()),
            "by_status": facets["statuses"],
            "by_type": facets["types"],
        },
        "lifecycle": _lifecycle(profile),
        "learned": [n.model_dump(mode="json") for n in norms],
        "checks": {
            "universal": watchers.catalog(),
            # rules this company added on top of the built-ins
            "profile": [
                {"name": w.get("name"), "summary": w.get("title", w.get("name"))}
                for w in profile.watchers
            ],
        },
        "can_do": _allowed_actions(profile),
        "practice_mode": bool(practice),
    }
