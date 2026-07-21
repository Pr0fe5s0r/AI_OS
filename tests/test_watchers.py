from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from packages.core import graph, watchers
from packages.core.db import Session
from packages.core.detect import _dedupe_by_entity, _event_candidates
from packages.shared.schema import Evidence, Situation

# Checkpoint 2, part C: universal built-in watcher primitives. None of these
# take a profile — they reason about graph topology, elapsed time, and a
# rhythm's own norm baseline, which exist for ANY company.

_CO = "test-watchers"


async def _reset(session) -> None:
    await graph.wipe_company(_CO)
    await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": _CO})
    await session.execute(text("DELETE FROM norm_baselines WHERE company_id = :c"), {"c": _CO})
    await session.commit()


# ------------------------------ stalled_thing ------------------------------


async def test_stalled_thing_fires_after_real_prior_activity_goes_quiet() -> None:
    await graph.bootstrap()
    async with Session() as session:
        await _reset(session)

    old = datetime.now(UTC) - timedelta(days=10)
    await graph.mirror_event(_CO, "stall-ev-1", old, "github")
    await graph.mirror_event(_CO, "stall-ev-2", old, "github")
    await graph.upsert_thing(_CO, "stall-thing-1", "Incident", "Stalled issue", status="open", last_activity=old)
    await graph.link_event_to_thing(_CO, "stall-ev-1", "stall-thing-1")
    await graph.link_event_to_thing(_CO, "stall-ev-2", "stall-thing-1")

    async with Session() as session:
        ids = {s.id for s in await watchers.stalled_things(session, _CO)}
    assert "stalled_thing:stall-thing-1" in ids


async def test_stalled_thing_ignores_terminal_status() -> None:
    await graph.bootstrap()
    async with Session() as session:
        await _reset(session)

    old = datetime.now(UTC) - timedelta(days=10)
    await graph.mirror_event(_CO, "closed-ev-1", old, "github")
    await graph.mirror_event(_CO, "closed-ev-2", old, "github")
    await graph.upsert_thing(_CO, "closed-thing-1", "Incident", "Closed issue", status="closed", last_activity=old)
    await graph.link_event_to_thing(_CO, "closed-ev-1", "closed-thing-1")
    await graph.link_event_to_thing(_CO, "closed-ev-2", "closed-thing-1")

    async with Session() as session:
        ids = {s.id for s in await watchers.stalled_things(session, _CO)}
    assert "stalled_thing:closed-thing-1" not in ids


async def test_stalled_thing_evidence_carries_the_real_url() -> None:
    """Regression test: a Thing's id is the id of the event that created it
    (resolve.py), so its Evidence.url must be joined back from Postgres. Found
    live — a one-click action built from evidence.url=None fails with
    "missing parameter 'repo'", because the move's URL template has nothing
    to extract repo/number from."""
    await graph.bootstrap()
    async with Session() as session:
        await _reset(session)

        old = datetime.now(UTC) - timedelta(days=10)
        await session.execute(
            text(
                """
                INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                     timestamp, content, content_tsv, metadata)
                VALUES ('url-thing-1', :c, 'github', 'issue', 'u', 'u', :ts, 'x', to_tsvector('x'), CAST(:md AS jsonb)),
                       ('url-ev-2', :c, 'github', 'issue', 'u', 'u', :ts, 'x', to_tsvector('x'), '{}'::jsonb)
                ON CONFLICT (id, timestamp) DO NOTHING
                """
            ),
            {"c": _CO, "ts": old, "md": json.dumps({"url": "https://github.com/acme/repo/issues/42"})},
        )
        await session.commit()

    await graph.mirror_event(_CO, "url-thing-1", old, "github")
    await graph.mirror_event(_CO, "url-ev-2", old, "github")
    await graph.upsert_thing(_CO, "url-thing-1", "Incident", "Stalled issue", status="open", last_activity=old)
    await graph.link_event_to_thing(_CO, "url-thing-1", "url-thing-1")
    await graph.link_event_to_thing(_CO, "url-ev-2", "url-thing-1")

    async with Session() as session:
        situations = await watchers.stalled_things(session, _CO)
    by_id = {s.id: s for s in situations}
    assert by_id["stalled_thing:url-thing-1"].evidence[0].url == "https://github.com/acme/repo/issues/42"


# ----------------------------- aging_commitment -----------------------------


async def test_aging_commitment_fires_on_single_event_thing() -> None:
    await graph.bootstrap()
    async with Session() as session:
        await _reset(session)

    old = datetime.now(UTC) - timedelta(days=10)
    await graph.mirror_event(_CO, "commit-ev-1", old, "github")
    await graph.upsert_thing(_CO, "commit-thing-1", "Incident", "Never followed up", status="open", last_activity=old)
    await graph.link_event_to_thing(_CO, "commit-ev-1", "commit-thing-1")

    async with Session() as session:
        ids = {s.id for s in await watchers.aging_commitments(session, _CO)}
    assert "aging_commitment:commit-thing-1" in ids


async def test_aging_commitment_ignores_things_with_followup_activity() -> None:
    """A second event means someone DID come back — that's stalled_thing's
    territory (if it goes quiet), not an abandoned single-shot commitment."""
    await graph.bootstrap()
    async with Session() as session:
        await _reset(session)

    old = datetime.now(UTC) - timedelta(days=10)
    await graph.mirror_event(_CO, "followup-ev-1", old, "github")
    await graph.mirror_event(_CO, "followup-ev-2", old, "github")
    await graph.upsert_thing(_CO, "followup-thing-1", "Incident", "Had a follow-up", status="open", last_activity=old)
    await graph.link_event_to_thing(_CO, "followup-ev-1", "followup-thing-1")
    await graph.link_event_to_thing(_CO, "followup-ev-2", "followup-thing-1")

    async with Session() as session:
        ids = {s.id for s in await watchers.aging_commitments(session, _CO)}
    assert "aging_commitment:followup-thing-1" not in ids


# ----------------------------- orphaned_hotspot -----------------------------


async def test_orphaned_hotspot_fires_on_heavily_mentioned_unresolved_thing() -> None:
    await graph.bootstrap()
    async with Session() as session:
        await _reset(session)

    now = datetime.now(UTC)
    await graph.upsert_thing(_CO, "hotspot-center", "Incident", "The hot one", status="open", last_activity=now)
    for i in range(3):
        other_id = f"hotspot-other-{i}"
        await graph.upsert_thing(_CO, other_id, "Incident", f"Mentioner {i}", status="open", last_activity=now)
        await graph.link_things(_CO, other_id, "hotspot-center", "MENTIONS", confidence=0.7, method="keyword")

    async with Session() as session:
        ids = {s.id for s in await watchers.orphaned_hotspots(session, _CO)}
    assert "orphaned_hotspot:hotspot-center" in ids


async def test_orphaned_hotspot_excludes_things_with_any_other_link() -> None:
    await graph.bootstrap()
    async with Session() as session:
        await _reset(session)

    now = datetime.now(UTC)
    await graph.upsert_thing(_CO, "resolved-center", "Incident", "The resolved one", status="open", last_activity=now)
    for i in range(3):
        other_id = f"resolved-other-{i}"
        await graph.upsert_thing(_CO, other_id, "Incident", f"Mentioner {i}", status="open", last_activity=now)
        await graph.link_things(_CO, other_id, "resolved-center", "MENTIONS", confidence=0.7, method="keyword")
    await graph.upsert_thing(_CO, "closer-1", "PullRequest", "The fix", status="open", last_activity=now)
    await graph.link_things(_CO, "closer-1", "resolved-center", "CLOSES", confidence=1.0, method="keyword")

    async with Session() as session:
        ids = {s.id for s in await watchers.orphaned_hotspots(session, _CO)}
    assert "orphaned_hotspot:resolved-center" not in ids


# ------------------------------- broken_rhythm -------------------------------

_RHYTHM = {"name": "issue_resolution_hours", "source": "github", "type": "issue", "end_field": "closed_at"}


async def _seed_baseline(session, median: float, std: float) -> None:
    await session.execute(
        text(
            """
            INSERT INTO norm_baselines (company_id, metric, unit, n, median, mean, std, window_days, computed_at, scope)
            VALUES (:c, 'issue_resolution_hours', 'hours', 10, :median, :median, :std, 90, now(), 'business')
            """
        ),
        {"c": _CO, "median": median, "std": std},
    )


async def test_broken_rhythm_fires_when_open_item_exceeds_norm_threshold() -> None:
    async with Session() as session:
        await _reset(session)
        await _seed_baseline(session, median=2.0, std=1.0)  # threshold = 2 + 2*1 = 4h
        old_ts = datetime.now(UTC) - timedelta(hours=10)
        await session.execute(
            text(
                """
                INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                     timestamp, content, metadata, content_tsv, backfilled)
                VALUES ('broken-ev-1', :c, 'github', 'issue', 'u', 'u', :ts, 'x', CAST(:md AS jsonb), to_tsvector('x'), false)
                ON CONFLICT (id, timestamp) DO NOTHING
                """
            ),
            {"c": _CO, "ts": old_ts, "md": json.dumps({"url": "https://github.com/acme/repo/issues/7"})},
        )
        await session.commit()

        situations = await watchers.broken_rhythms(session, _CO, [_RHYTHM])

    by_id = {s.id: s for s in situations}
    assert "broken_rhythm:issue_resolution_hours:broken-ev-1" in by_id
    # regression: this evidence feeds a one-click action's URL template
    # (repo/number extraction) — url=None made every apply_label/assign_issue
    # click fail with "missing parameter 'repo'"
    assert by_id["broken_rhythm:issue_resolution_hours:broken-ev-1"].evidence[0].url == \
        "https://github.com/acme/repo/issues/7"


async def test_broken_rhythm_ignores_already_resolved_items() -> None:
    async with Session() as session:
        await _reset(session)
        await _seed_baseline(session, median=2.0, std=1.0)
        old_ts = datetime.now(UTC) - timedelta(hours=10)
        await session.execute(
            text(
                """
                INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                     timestamp, content, metadata, content_tsv, backfilled)
                VALUES ('broken-ev-2', :c, 'github', 'issue', 'u', 'u', :ts, 'x', CAST(:md AS jsonb), to_tsvector('x'), false)
                ON CONFLICT (id, timestamp) DO NOTHING
                """
            ),
            {"c": _CO, "ts": old_ts, "md": json.dumps({"closed_at": datetime.now(UTC).isoformat()})},
        )
        await session.commit()

        situations = await watchers.broken_rhythms(session, _CO, [_RHYTHM])

    ids = {s.id for s in situations}
    assert "broken_rhythm:issue_resolution_hours:broken-ev-2" not in ids


async def test_broken_rhythm_ignores_backfilled_events() -> None:
    async with Session() as session:
        await _reset(session)
        await _seed_baseline(session, median=2.0, std=1.0)
        old_ts = datetime.now(UTC) - timedelta(hours=10)
        await session.execute(
            text(
                """
                INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                     timestamp, content, metadata, content_tsv, backfilled)
                VALUES ('broken-ev-3', :c, 'github', 'issue', 'u', 'u', :ts, 'x', '{}'::jsonb, to_tsvector('x'), true)
                ON CONFLICT (id, timestamp) DO NOTHING
                """
            ),
            {"c": _CO, "ts": old_ts},
        )
        await session.commit()

        situations = await watchers.broken_rhythms(session, _CO, [_RHYTHM])

    ids = {s.id for s in situations}
    assert "broken_rhythm:issue_resolution_hours:broken-ev-3" not in ids


# ------------------------------ volume_anomaly ------------------------------


async def test_volume_anomaly_fires_on_sustained_spike() -> None:
    async with Session() as session:
        await _reset(session)
        await session.execute(
            text(
                """
                INSERT INTO norm_baselines (company_id, metric, unit, n, median, mean, std, window_days, computed_at, scope)
                VALUES (:c, 'github_events_per_hour', 'events/hour', 30, 5.0, 5.0, 1.0, 14, now(), 'system')
                """
            ),
            {"c": _CO},
        )
        now = datetime.now(UTC)
        for i in range(20):  # 20 in the last hour vs. a normal ~5, std 1 -> way past k=3
            await session.execute(
                text(
                    """
                    INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                         timestamp, content, content_tsv, backfilled)
                    VALUES (:id, :c, 'github', 'issue', 'u', 'u', :ts, 'x', to_tsvector('x'), false)
                    ON CONFLICT (id, timestamp) DO NOTHING
                    """
                ),
                {"id": f"vol-ev-{i}", "c": _CO, "ts": now - timedelta(minutes=5)},
            )
        await session.commit()

        situations = await watchers.volume_anomalies(session, _CO, ["github"])

    ids = {s.id for s in situations}
    assert f"volume_anomaly:{_CO}:github" in ids


async def test_volume_anomaly_needs_a_mature_baseline() -> None:
    """Fewer than VOLUME_ANOMALY_MIN_OBSERVATIONS hourly buckets -> too soon to judge."""
    async with Session() as session:
        await _reset(session)
        await session.execute(
            text(
                """
                INSERT INTO norm_baselines (company_id, metric, unit, n, median, mean, std, window_days, computed_at, scope)
                VALUES (:c, 'github_events_per_hour', 'events/hour', 3, 5.0, 5.0, 1.0, 14, now(), 'system')
                """
            ),
            {"c": _CO},
        )
        await session.commit()

        situations = await watchers.volume_anomalies(session, _CO, ["github"])

    assert situations == []


# --------------------------- live-only + dedupe glue ---------------------------


async def test_event_candidates_are_live_only_not_backfilled() -> None:
    async with Session() as session:
        await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": _CO})
        now = datetime.now(UTC)
        await session.execute(
            text(
                """
                INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                     timestamp, content, content_tsv, backfilled)
                VALUES ('live-ev', :c, 'github', 'issue', 'u', 'u', :ts, 'x', to_tsvector('x'), false),
                       ('old-ev', :c, 'github', 'issue', 'u', 'u', :ts, 'x', to_tsvector('x'), true)
                ON CONFLICT (id, timestamp) DO NOTHING
                """
            ),
            {"c": _CO, "ts": now},
        )
        await session.commit()

        candidates = await _event_candidates(session, _CO, {"select": {"source": "github", "type": "issue"}}, now)

    ids = {c["id"] for c in candidates}
    assert "live-ev" in ids
    assert "old-ev" not in ids


def test_dedupe_by_entity_keeps_highest_severity() -> None:
    """Two DIFFERENT origins (a universal primitive + a profile watcher, or
    two universal primitives) matching the same entity must collapse to one
    card — the fix already proven live earlier this session, reused here."""
    now = datetime.now(UTC)
    ev = Evidence(event_id="dupe-1", source="graph", timestamp=now, excerpt="x")
    low = Situation(
        id="stalled_thing:dupe-1", company_id=_CO, rule="stalled_thing", severity="medium",
        title="Stalled", summary="", evidence=[ev], status="open", created_at=now, kind="business",
    )
    high = Situation(
        id="broken_rhythm:issue_resolution_hours:dupe-1", company_id=_CO, rule="broken_rhythm", severity="high",
        title="Broken rhythm", summary="", evidence=[ev], status="open", created_at=now, kind="business",
    )

    result = _dedupe_by_entity([low, high])
    assert len(result) == 1
    assert result[0].severity == "high"
