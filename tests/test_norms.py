from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from packages.core.db import Session
from packages.core.norms import (
    compute_baselines,
    compute_volume_baseline,
    current_hourly_volume,
    field_presence,
    learn_norms,
    norm_evidence,
)


def test_learn_norms_returns_baseline() -> None:
    baseline = learn_norms(
        {"name": "ticket_resolution_hours", "unit": "hours", "window_days": 120},
        [4.0, 4.0, 4.0, 4.0, 4.0],
    )
    assert baseline.metric == "ticket_resolution_hours"
    assert baseline.unit == "hours"
    assert baseline.n == 5
    assert baseline.median == 4.0
    assert baseline.mean == 4.0
    assert baseline.std == 0.0
    assert baseline.trend_per_period == 0.0
    assert baseline.maturity == "learning"
    assert baseline.scope == "business"


def test_learn_norms_handles_empty() -> None:
    baseline = learn_norms({"name": "pr_review_hours", "unit": "hours"}, [])
    assert baseline.n == 0
    assert baseline.median == 0.0
    assert baseline.maturity == "insufficient"


def test_learn_norms_scope_is_data_not_hardcoded() -> None:
    baseline = learn_norms({"name": "github_events_per_hour", "unit": "events/hour", "scope": "system"}, [1.0])
    assert baseline.scope == "system"


# ------------------------------ trend-aware + outlier-trimmed (CP2 part B) ------------------------------


def test_learn_norms_trims_outliers() -> None:
    """30.0 is a wild outlier next to a tight cluster — it must not drag the
    median/std around, even though it still counts toward the raw sample size."""
    baseline = learn_norms(
        {"name": "ticket_resolution_hours", "unit": "hours", "window_days": 120},
        [2.0, 4.0, 6.0, 8.0, 30.0],
    )
    assert baseline.n == 5  # raw count is unaffected — maturity still sees all 5
    assert baseline.median == 5.0  # computed on [2, 4, 6, 8] — 30.0 excluded by IQR trimming
    assert baseline.std < 5.0  # nowhere near what an untrimmed std would be


def test_learn_norms_is_trend_aware_not_flat_mean() -> None:
    """A sustained shift from 1.0 to 10.0 (chronological: low half, then high
    half) must pull `mean` toward the RECENT level, not blend the whole window."""
    observations = [1.0] * 10 + [10.0] * 10
    flat_mean = sum(observations) / len(observations)  # 5.5 — what a naive mean would say

    baseline = learn_norms({"name": "issue_resolution_hours", "unit": "hours"}, observations)

    assert baseline.trend_per_period > 0
    assert baseline.mean > flat_mean


def test_learn_norms_maturity_gates_on_sample_count() -> None:
    assert learn_norms({"name": "m"}, [1.0, 1.0]).maturity == "insufficient"
    assert learn_norms({"name": "m"}, [1.0] * 10).maturity == "learning"
    assert learn_norms({"name": "m"}, [1.0] * 25).maturity == "stable"


# ------------------------------ volume baseline (system) ------------------------------

_CO = "test-norms-volume"


async def _seed_hourly_events(session, n_per_hour: int, hours_ago_start: int, hours_ago_end: int) -> None:
    now = datetime.now(UTC)
    rows = []
    for h in range(hours_ago_end, hours_ago_start):
        ts = now - timedelta(hours=h, minutes=5)
        for i in range(n_per_hour):
            rows.append((f"vol-{h}-{i}", ts))
    for eid, ts in rows:
        await session.execute(
            text(
                """
                INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                     timestamp, content, content_tsv)
                VALUES (:id, :c, 'github', 'issue', 'u', 'u', :ts, 'x', to_tsvector('x'))
                ON CONFLICT (company_id, id, timestamp) DO NOTHING
                """
            ),
            {"id": eid, "c": _CO, "ts": ts},
        )


async def test_volume_baseline_reflects_the_real_hourly_rate() -> None:
    async with Session() as session:
        await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": _CO})
        await session.commit()
        await _seed_hourly_events(session, n_per_hour=10, hours_ago_start=48, hours_ago_end=1)
        await session.commit()

        baseline = await compute_volume_baseline(session, _CO, "github", window_days=14)
        await session.commit()
        assert baseline.n >= 40  # ~47 complete hourly buckets seeded
        assert baseline.median == 10.0
        assert baseline.scope == "system"
        assert baseline.metric == "github_events_per_hour"


async def test_current_hourly_volume_counts_only_the_last_hour() -> None:
    async with Session() as session:
        await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": _CO})
        await session.commit()
        await _seed_hourly_events(session, n_per_hour=3, hours_ago_start=1, hours_ago_end=0)
        await session.commit()

        current = await current_hourly_volume(session, _CO, "github")
        assert current == 3


# ------------------------- things we cannot measure -------------------------
# A rhythm's end_field that has NEVER appeared, despite enough history to rule
# out "just hasn't happened yet", must read as "unmeasurable" — not blend
# silently into "insufficient" (still learning), which falsely implies that
# waiting longer will fix it.

_CO_UNM = "test-norms-unmeasurable"
_DEFN = {
    "name": "issue_resolution_hours", "source": "github", "type": "issue",
    "end_field": "resolved_at", "unit": "hours", "window_days": 90,
}


async def _seed_issues(session, company_id: str, count: int, oldest_days: float, populated: int = 0) -> None:
    """`count` issue events spread from `oldest_days` ago to nearly now. The
    first `populated` (oldest) get a real resolved_at in their metadata."""
    now = datetime.now(UTC)
    for i in range(count):
        age = oldest_days * (count - i) / count if count else 0.0
        ts = now - timedelta(days=age)
        md = {"resolved_at": (ts + timedelta(hours=1)).isoformat()} if i < populated else {}
        await session.execute(
            text(
                """
                INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                     timestamp, content, content_tsv, metadata)
                VALUES (:id, :c, 'github', 'issue', 'u', 'u', :ts, 'x', to_tsvector('x'), CAST(:md AS jsonb))
                ON CONFLICT (company_id, id, timestamp) DO NOTHING
                """
            ),
            {"id": f"unm-{i}", "c": company_id, "ts": ts, "md": json.dumps(md)},
        )


async def _reset_unm(session) -> None:
    await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": _CO_UNM})
    await session.execute(text("DELETE FROM norm_baselines WHERE company_id = :c"), {"c": _CO_UNM})
    await session.commit()


async def test_field_presence_reports_ever_populated_and_history() -> None:
    async with Session() as session:
        await _reset_unm(session)
        await _seed_issues(session, _CO_UNM, count=12, oldest_days=20.0, populated=0)
        await session.commit()

        presence = await field_presence(session, _CO_UNM, "github", "issue", "resolved_at")
        assert presence["total_events"] == 12
        assert presence["ever_populated"] is False
        assert presence["oldest_days"] is not None and presence["oldest_days"] >= 19.0


async def test_unmeasurable_when_field_never_populated_with_enough_history() -> None:
    async with Session() as session:
        await _reset_unm(session)
        await _seed_issues(session, _CO_UNM, count=12, oldest_days=20.0, populated=0)
        await session.commit()

        baselines = await compute_baselines(session, _CO_UNM, [_DEFN])
        await session.commit()
        assert baselines[0].n == 0
        assert baselines[0].maturity == "unmeasurable"


async def test_too_few_events_stays_insufficient_not_unmeasurable() -> None:
    async with Session() as session:
        await _reset_unm(session)
        await _seed_issues(session, _CO_UNM, count=3, oldest_days=20.0, populated=0)
        await session.commit()

        baselines = await compute_baselines(session, _CO_UNM, [_DEFN])
        await session.commit()
        assert baselines[0].maturity == "insufficient"


async def test_too_recent_stays_insufficient_not_unmeasurable() -> None:
    """A company that connected yesterday must not be told its data is
    unmeasurable just because nothing has finished yet."""
    async with Session() as session:
        await _reset_unm(session)
        await _seed_issues(session, _CO_UNM, count=12, oldest_days=2.0, populated=0)
        await session.commit()

        baselines = await compute_baselines(session, _CO_UNM, [_DEFN])
        await session.commit()
        assert baselines[0].maturity == "insufficient"


async def test_a_single_real_example_reverts_unmeasurable() -> None:
    """Self-healing: once the field is populated even once, the override
    stops firing on its own — no reset or manual step required."""
    async with Session() as session:
        await _reset_unm(session)
        await _seed_issues(session, _CO_UNM, count=12, oldest_days=20.0, populated=1)
        await session.commit()

        baselines = await compute_baselines(session, _CO_UNM, [_DEFN])
        await session.commit()
        assert baselines[0].maturity != "unmeasurable"


async def test_norm_evidence_carries_the_unmeasurable_reason() -> None:
    async with Session() as session:
        await _reset_unm(session)
        await _seed_issues(session, _CO_UNM, count=12, oldest_days=20.0, populated=0)
        await session.commit()

        result = await norm_evidence(session, _CO_UNM, _DEFN)
        assert result["maturity"] == "unmeasurable"
        assert result["unmeasurable_reason"] is not None
        assert "resolved_at" in result["unmeasurable_reason"]


async def test_norm_evidence_reason_is_none_when_measurable() -> None:
    async with Session() as session:
        await _reset_unm(session)
        await _seed_issues(session, _CO_UNM, count=12, oldest_days=20.0, populated=6)
        await session.commit()

        result = await norm_evidence(session, _CO_UNM, _DEFN)
        assert result["maturity"] != "unmeasurable"
        assert result["unmeasurable_reason"] is None
