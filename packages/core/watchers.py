from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import graph
from packages.core.norms import current_hourly_volume, get_norms
from packages.core.store import get_events_by_ids
from packages.shared.schema import Evidence, Situation

# Universal, profile-free watcher primitives (checkpoint 2, part C). Unlike
# packages.core.detect's Tier-1 rules (which come from profile.watchers),
# none of these need a watcher declared anywhere — they reason about
# structure that exists for ANY company in ANY industry: graph topology,
# elapsed time, and a rhythm's own norm baseline. Both origins feed the SAME
# situations list and the SAME highest-severity-wins dedupe in detect.py.

STALL_DAYS = 3
MIN_STALL_EVENTS = 2
COMMITMENT_MIN_AGE_DAYS = 7
HOTSPOT_MIN_MENTIONS = 3
VOLUME_ANOMALY_K = 3.0
VOLUME_ANOMALY_MIN_OBSERVATIONS = 24
BROKEN_RHYTHM_K = 2.0

# A small, generic terminal-state vocabulary — common English for "this is
# done" that already appears across this codebase's real connectors, not a
# specific industry's words.
_TERMINAL_STATUSES = ("closed", "done", "resolved", "solved", "merged", "cancelled", "canceled")


def catalog() -> list[dict[str, Any]]:
    """What the built-ins look for, as DATA — so a screen can tell a person
    what the system is watching for without the frontend hardcoding a list
    that silently goes stale when a watcher changes.

    Descriptions stay domain-free ("a record", not "an issue"); the caller
    substitutes the company's own noun from its vocabulary.
    """
    return [
        {
            "name": "stalled_thing",
            "summary": "Was being worked on, then went quiet",
            "detail": f"No activity for more than {STALL_DAYS} days, after real prior activity.",
        },
        {
            "name": "aging_commitment",
            "summary": "Opened once, never touched again",
            "detail": f"Created more than {COMMITMENT_MIN_AGE_DAYS} days ago with no follow-up at all.",
        },
        {
            "name": "orphaned_hotspot",
            "summary": "Everyone mentions it, nobody owns it",
            "detail": f"Referenced by {HOTSPOT_MIN_MENTIONS}+ other records, but nothing is acting on it.",
        },
        {
            "name": "broken_rhythm",
            "summary": "Taking far longer than your normal",
            "detail": "Still open past your own learned typical time, by a wide margin.",
        },
        {
            "name": "volume_anomaly",
            "summary": "Unusual amount of activity",
            "detail": "This hour's volume is far above or below what this source normally does.",
        },
    ]


async def _urls_by_id(session: AsyncSession, company_id: str, ids: list[str]) -> dict[str, str | None]:
    """A Thing's id is the id of the event that created it (see resolve.py),
    so this is the same join used to show a Thing's real page: without it,
    Evidence.url stays null and any one-click action built from it (apply a
    label, assign someone) fails with "missing parameter 'repo'" — the move's
    URL template has nothing to extract repo/number from."""
    events = await get_events_by_ids(session, company_id, ids)
    return {e.id: (e.metadata or {}).get("url") for e in events}


async def stalled_things(session: AsyncSession, company_id: str) -> list[Situation]:
    rows = await graph.stalled_things(company_id, STALL_DAYS, list(_TERMINAL_STATUSES), MIN_STALL_EVENTS)
    urls = await _urls_by_id(session, company_id, [r["id"] for r in rows])
    now = datetime.now(UTC)
    return [
        Situation(
            id=f"stalled_thing:{r['id']}",
            company_id=company_id,
            rule="stalled_thing",
            severity="medium",
            title="Stalled: no activity in a while",
            summary=f"\"{r['title'] or r['id']}\" has had no activity for over {STALL_DAYS} days, after being actively worked on.",
            recommended_action="Check in, or close it out if it's no longer relevant.",
            evidence=[Evidence(event_id=r["id"], source="graph", timestamp=now, excerpt=r["title"] or r["id"], url=urls.get(r["id"]))],
            status="open",
            created_at=now,
            kind="business",
        )
        for r in rows
    ]


async def aging_commitments(session: AsyncSession, company_id: str) -> list[Situation]:
    rows = await graph.single_event_aging_things(company_id, COMMITMENT_MIN_AGE_DAYS)
    urls = await _urls_by_id(session, company_id, [r["id"] for r in rows])
    now = datetime.now(UTC)
    return [
        Situation(
            id=f"aging_commitment:{r['id']}",
            company_id=company_id,
            rule="aging_commitment",
            severity="medium",
            title="Opened, then never touched again",
            summary=(
                f"\"{r['title'] or r['id']}\" was created over {COMMITMENT_MIN_AGE_DAYS} "
                "days ago and has had no follow-up since."
            ),
            recommended_action="Confirm this is still needed, or close it.",
            evidence=[Evidence(event_id=r["id"], source="graph", timestamp=now, excerpt=r["title"] or r["id"], url=urls.get(r["id"]))],
            status="open",
            created_at=now,
            kind="business",
        )
        for r in rows
    ]


async def orphaned_hotspots(session: AsyncSession, company_id: str) -> list[Situation]:
    rows = await graph.hotspot_things(company_id, HOTSPOT_MIN_MENTIONS)
    urls = await _urls_by_id(session, company_id, [r["id"] for r in rows])
    now = datetime.now(UTC)
    out = []
    for r in rows:
        count = r["mention_count"]
        out.append(
            Situation(
                id=f"orphaned_hotspot:{r['id']}",
                company_id=company_id,
                rule="orphaned_hotspot",
                severity="high",
                title="Talked about a lot, resolved by nothing",
                summary=(
                    f"\"{r['title'] or r['id']}\" is referenced by {count} other "
                    f"item{'s' if count != 1 else ''}, but nothing has acted on it."
                ),
                recommended_action="This is getting attention from multiple places — worth owning explicitly.",
                evidence=[Evidence(event_id=r["id"], source="graph", timestamp=now, excerpt=r["title"] or r["id"], url=urls.get(r["id"]))],
                status="open",
                created_at=now,
                kind="business",
            )
        )
    return out


async def broken_rhythms(session: AsyncSession, company_id: str, rhythms: list[dict]) -> list[Situation]:
    """For each declared rhythm, any still-open item — structurally, one
    whose metadata[end_field] is unset, never a profile-specific 'what does
    open mean' lookup — whose age has passed median + k*std of that
    rhythm's own norm baseline."""
    now = datetime.now(UTC)
    baselines = {b.metric: b for b in await get_norms(session, company_id, scope="business")}
    out: list[Situation] = []
    for defn in rhythms:
        baseline = baselines.get(defn["name"])
        if baseline is None or baseline.n == 0:
            continue
        threshold_hours = baseline.median + BROKEN_RHYTHM_K * baseline.std
        rows = await session.execute(
            text(
                "SELECT id, timestamp, metadata, content FROM events "
                "WHERE company_id = :c AND source = :s AND type = :t AND backfilled = false"
            ),
            {"c": company_id, "s": defn["source"], "t": defn["type"]},
        )
        for r in rows:
            md = r.metadata if isinstance(r.metadata, dict) else json.loads(r.metadata)
            if (md or {}).get(defn["end_field"]) is not None:
                continue  # already resolved — structurally, not by a status string
            ts = r.timestamp if r.timestamp.tzinfo else r.timestamp.replace(tzinfo=UTC)
            age_hours = (now - ts).total_seconds() / 3600.0
            if age_hours <= threshold_hours:
                continue
            out.append(
                Situation(
                    id=f"broken_rhythm:{defn['name']}:{r.id}",
                    company_id=company_id,
                    rule="broken_rhythm",
                    severity="high",
                    title=f"Past its normal {defn['name'].replace('_', ' ')}",
                    summary=(
                        f"Open for {round(age_hours, 1)}h, vs. a typical {baseline.median}h "
                        f"({baseline.maturity} baseline, n={baseline.n})."
                    ),
                    recommended_action="Check what's blocking it.",
                    evidence=[
                        Evidence(
                            event_id=r.id, source=defn["source"], timestamp=r.timestamp,
                            excerpt=(r.content or "")[:200], url=(md or {}).get("url"),
                        )
                    ],
                    status="open",
                    created_at=now,
                    kind="business",
                )
            )
    return out


async def volume_anomalies(session: AsyncSession, company_id: str, sources: list[str]) -> list[Situation]:
    """A source's event volume this hour deviates sharply, in EITHER
    direction, from its own recent norm. Reads the same volume baseline
    connector self-monitoring (checkpoint 6, part A) already maintains every
    15 minutes — one signal, two consumers — rather than recomputing it."""
    now = datetime.now(UTC)
    system_norms = {b.metric: b for b in await get_norms(session, company_id, scope="system")}
    out: list[Situation] = []
    for source in sources:
        baseline = system_norms.get(f"{source}_events_per_hour")
        if baseline is None or baseline.n < VOLUME_ANOMALY_MIN_OBSERVATIONS or baseline.std <= 0:
            continue
        current = await current_hourly_volume(session, company_id, source)
        deviation = abs(current - baseline.median) / baseline.std
        if deviation < VOLUME_ANOMALY_K:
            continue
        direction = "more" if current > baseline.median else "fewer"
        out.append(
            Situation(
                id=f"volume_anomaly:{company_id}:{source}",
                company_id=company_id,
                rule="volume_anomaly",
                severity="medium",
                title=f"Unusual {source} activity this hour",
                summary=(
                    f"{current} {source} events in the last hour vs. a normal "
                    f"~{round(baseline.median)} — {round(deviation, 1)}x the usual spread, "
                    f"{direction} than usual."
                ),
                recommended_action="Worth a look — something changed.",
                evidence=[],
                status="open",
                created_at=now,
                kind="business",
            )
        )
    return out
