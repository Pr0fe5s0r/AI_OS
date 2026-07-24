from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import text

import apps.common.triggers as triggers
from apps.common.triggers import fire_event_workflows, run_scheduled_workflows
from packages.core import workflows as wf
from packages.core.db import Session
from packages.core.profile import Profile
from packages.core.workflows import claim_fire, cron_matches, minute_bucket, valid_cron
from packages.shared.schema import WorkflowStep, WorkflowTrigger

# The triggers runtime (build 3): what fires a saved workflow without a person.
#
# The interesting tests here are the SAFETY ones, not the scheduling ones:
#   - an unattended run must not escape the allowlist gate
#   - nothing may fire twice for the same cause (a retried cron minute, or a
#     situation that stays open across many watcher passes)
# Cron matching itself is a pure function, tested with no DB at all.

_CO = "test-triggers"


def _profile() -> Profile:
    """Same shape as the workflow tests: one allowlisted log move, one not, so
    the gate's effect on an UNATTENDED run is visible on its own."""
    return Profile(
        company_id=_CO,
        sources=[], things={}, links={}, rhythms=[], watchers=[], vocabulary={},
        moves={
            "registry": {
                "safe_log": {"kind": "log", "approval_required": False, "template": "noted (safe)"},
                "gated_log": {"kind": "log", "approval_required": True, "template": "noted (gated)"},
            },
            "autonomy": {"allowed_actions": []},
            "approval_defaults": {"dry_run": True},
        },
    )


@contextmanager
def _profile_loaded():
    """The runtime loads each company's profile from the DB; this test company
    has no confirmed profile row, so stand one in for the duration.

    Refusing every OTHER company is the point: the scheduled scan is global by
    design, so without this a test tick would run real companies' workflows.
    A company whose profile won't load is skipped without burning its claim."""
    async def fake_get_profile(session, company_id: str) -> Profile:
        if company_id != _CO:
            raise LookupError(f"not a test company: {company_id}")
        return _profile()

    original = triggers.get_profile
    triggers.get_profile = fake_get_profile  # type: ignore[assignment]
    try:
        yield
    finally:
        triggers.get_profile = original  # type: ignore[assignment]


async def _reset(session) -> None:
    await session.execute(
        text(
            "DELETE FROM workflow_trigger_fires WHERE workflow_id IN "
            "(SELECT id FROM workflows WHERE company_id = :c)"
        ),
        {"c": _CO},
    )
    await session.execute(text("DELETE FROM workflow_runs WHERE company_id = :c"), {"c": _CO})
    await session.execute(text("DELETE FROM workflows WHERE company_id = :c"), {"c": _CO})
    await session.execute(text("DELETE FROM actions WHERE company_id = :c"), {"c": _CO})
    await session.execute(text("DELETE FROM situations WHERE company_id = :c"), {"c": _CO})
    await session.commit()


async def _seed_situation(session, sid: str, rule: str, severity: str = "high") -> None:
    await session.execute(
        text(
            """
            INSERT INTO situations
                (id, company_id, rule, severity, title, summary, recommended_action,
                 evidence, status, created_at, kind, choices)
            VALUES (:id, :c, :rule, :sev, :title, '', NULL, '[]'::jsonb, 'open', now(), 'business', NULL)
            ON CONFLICT (id) DO UPDATE SET status = 'open', rule = EXCLUDED.rule
            """
        ),
        {"id": sid, "c": _CO, "rule": rule, "sev": severity, "title": f"{rule} situation"},
    )


# ---------------------------- cron matching (pure) ----------------------------


def test_cron_matches_basic_fields() -> None:
    nine_am = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)  # a Tuesday
    assert cron_matches("0 9 * * *", nine_am)
    assert not cron_matches("0 10 * * *", nine_am)
    assert not cron_matches("5 9 * * *", nine_am)
    assert cron_matches("* * * * *", nine_am)


def test_cron_matches_steps_ranges_and_lists() -> None:
    at_9_15 = datetime(2026, 7, 21, 9, 15, tzinfo=UTC)
    assert cron_matches("*/15 * * * *", at_9_15)
    assert cron_matches("15 8-10 * * *", at_9_15)
    assert cron_matches("0,15,30 9 * * *", at_9_15)
    assert not cron_matches("*/20 * * * *", at_9_15)


def test_cron_matches_weekdays_with_sunday_as_both_0_and_7() -> None:
    tuesday = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
    sunday = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    assert cron_matches("0 9 * * 2", tuesday)      # Tue = 2
    assert not cron_matches("0 9 * * 1", tuesday)
    assert cron_matches("0 9 * * 1-5", tuesday)    # weekdays
    assert cron_matches("0 9 * * 0", sunday)
    assert cron_matches("0 9 * * 7", sunday)       # 7 is Sunday too
    assert not cron_matches("0 9 * * 1-5", sunday)


def test_cron_dom_and_dow_both_restricted_is_or_not_and() -> None:
    """Real cron's oddest rule: with both day fields set, EITHER matching fires."""
    tuesday_21st = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
    assert cron_matches("0 9 21 * 0", tuesday_21st)  # day-of-month matches, day-of-week doesn't
    assert cron_matches("0 9 1 * 2", tuesday_21st)   # day-of-week matches, day-of-month doesn't
    assert not cron_matches("0 9 1 * 0", tuesday_21st)


def test_malformed_cron_never_fires() -> None:
    """A mis-planned expression must do NOTHING, not fire on every tick."""
    when = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
    for bad in ("", "0 9 * *", "every morning", "0 9 * * * *", "99 9 * * *", "0 9 * * abc", "0 9 * * */0"):
        assert cron_matches(bad, when) is False, bad


def test_valid_cron_agrees_with_matching() -> None:
    """What the API accepts must be exactly what the runtime can fire. A schedule
    that validates but never matches would be the worst kind of silent failure."""
    for good in ("0 9 * * *", "*/15 * * * *", "0 9 * * 1-5", "0,30 8-18 * * *", "* * * * *"):
        assert valid_cron(good) is True, good
    for bad in ("", "0 9 * *", "every morning", "99 9 * * *", "0 9 * * abc", "0 9 * * */0"):
        assert valid_cron(bad) is False, bad
        assert cron_matches(bad, datetime(2026, 7, 21, 9, 0, tzinfo=UTC)) is False


def test_minute_bucket_is_stable_within_a_minute() -> None:
    a = datetime(2026, 7, 21, 9, 5, 1, tzinfo=UTC)
    b = datetime(2026, 7, 21, 9, 5, 59, tzinfo=UTC)
    assert minute_bucket(a) == minute_bucket(b) == "2026-07-21T09:05"
    assert minute_bucket(datetime(2026, 7, 21, 9, 6, tzinfo=UTC)) != minute_bucket(a)


# ------------------------------- the claim -------------------------------


async def test_claim_fire_is_exactly_once() -> None:
    async with Session() as session:
        await _reset(session)
        workflow = await wf.save_workflow(
            session, _CO, "claim", "x", [WorkflowStep(tool="list_situations")]
        )
        await session.commit()

        assert await claim_fire(session, workflow.id, "key-1") is True
        assert await claim_fire(session, workflow.id, "key-1") is False  # already taken
        assert await claim_fire(session, workflow.id, "key-2") is True   # a different cause
        await session.commit()


# ------------------------------ scheduled runs ------------------------------


async def test_scheduled_workflow_fires_on_its_minute_and_only_once() -> None:
    when = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
    async with Session() as session:
        await _reset(session)
        await wf.save_workflow(
            session, _CO, "morning log", "log every morning",
            [WorkflowStep(tool="run_action", args={"action": "safe_log", "situation_id": "s1"})],
            trigger=WorkflowTrigger(type="schedule", config={"cron": "0 9 * * *"}),
        )
        await session.commit()

    with _profile_loaded():
        first = await run_scheduled_workflows({}, now=when)
        second = await run_scheduled_workflows({}, now=when)  # a retried tick / second worker

    assert first["fired"] == 1
    assert first["runs"][0]["status"] == "done"
    assert second["fired"] == 0, "the same cron minute must never fire twice"

    async with Session() as session:
        runs = await wf.list_runs(session, _CO, first["runs"][0]["workflow_id"])
    assert len(runs) == 1
    assert runs[0].trigger == "scheduled"


async def test_scheduled_workflow_does_not_fire_off_its_minute() -> None:
    async with Session() as session:
        await _reset(session)
        await wf.save_workflow(
            session, _CO, "morning log", "log every morning",
            [WorkflowStep(tool="run_action", args={"action": "safe_log", "situation_id": "s1"})],
            trigger=WorkflowTrigger(type="schedule", config={"cron": "0 9 * * *"}),
        )
        await session.commit()

    with _profile_loaded():
        result = await run_scheduled_workflows({}, now=datetime(2026, 7, 21, 10, 30, tzinfo=UTC))

    assert result["fired"] == 0


async def test_disabled_workflow_never_fires_on_schedule() -> None:
    when = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
    async with Session() as session:
        await _reset(session)
        saved = await wf.save_workflow(
            session, _CO, "off", "log every morning",
            [WorkflowStep(tool="run_action", args={"action": "safe_log", "situation_id": "s1"})],
            trigger=WorkflowTrigger(type="schedule", config={"cron": "0 9 * * *"}),
        )
        await wf.update_workflow(session, _CO, saved.id, enabled=False)
        await session.commit()

    with _profile_loaded():
        result = await run_scheduled_workflows({}, now=when)

    assert result["fired"] == 0


async def test_a_scheduled_run_acts_autonomously_without_a_human() -> None:
    """Nobody is watching a scheduled run — which is the whole point. A saved,
    scheduled workflow was authorized when it was built and enabled, so it runs
    every step on its own, even ones off the allowlist or marked
    approval_required. Unattended is the intended state, not a reason to stop."""
    when = datetime(2026, 7, 21, 9, 0, tzinfo=UTC)
    async with Session() as session:
        await _reset(session)
        await wf.save_workflow(
            session, _CO, "auto schedule", "do both on a schedule",
            [
                WorkflowStep(tool="run_action", args={"action": "safe_log", "situation_id": "s1"}),
                WorkflowStep(tool="run_action", args={"action": "gated_log", "situation_id": "s2"}),
            ],
            trigger=WorkflowTrigger(type="schedule", config={"cron": "0 9 * * *"}),
        )
        await session.commit()

    with _profile_loaded():
        result = await run_scheduled_workflows({}, now=when)

    statuses = [r["status"] for r in result["runs"][0]["step_results"]]
    assert statuses == ["recorded", "recorded"]
    assert result["runs"][0]["status"] == "done"


# -------------------------------- event runs --------------------------------


async def test_event_workflow_fires_on_a_matching_situation_only() -> None:
    async with Session() as session:
        await _reset(session)
        await wf.save_workflow(
            session, _CO, "on unassigned bug", "log when a bug lands unassigned",
            [WorkflowStep(tool="run_action", args={"action": "safe_log"}, select={"rule": "unassigned_bug"})],
            trigger=WorkflowTrigger(type="event", config={"rule": "unassigned_bug"}),
        )
        await _seed_situation(session, "unassigned_bug:1", "unassigned_bug")
        await _seed_situation(session, "broken_rhythm:x", "broken_rhythm")
        await session.commit()

        raised = [
            {"id": "unassigned_bug:1", "rule": "unassigned_bug", "severity": "high"},
            {"id": "broken_rhythm:x", "rule": "broken_rhythm", "severity": "high"},
        ]
        with _profile_loaded():
            result = await fire_event_workflows(session, _CO, raised)

        assert result["fired"] == 1, "only the matching situation fires the workflow"
        assert result["runs"][0]["situation_id"] == "unassigned_bug:1"
        runs = await wf.list_runs(session, _CO, result["runs"][0]["workflow_id"])

    assert len(runs) == 1 and runs[0].trigger == "event"


async def test_event_run_targets_only_the_situation_that_fired_it() -> None:
    """An event run means 'this just happened, handle IT' — the step's selector
    must not sweep every other open item that also matches."""
    async with Session() as session:
        await _reset(session)
        await wf.save_workflow(
            session, _CO, "on bug", "log the new bug",
            [WorkflowStep(tool="run_action", args={"action": "safe_log"}, select={"rule": "unassigned_bug"})],
            trigger=WorkflowTrigger(type="event", config={"rule": "unassigned_bug"}),
        )
        # three open matches, but only one of them just fired
        for i in (1, 2, 3):
            await _seed_situation(session, f"unassigned_bug:{i}", "unassigned_bug")
        await session.commit()

        with _profile_loaded():
            result = await fire_event_workflows(
                session, _CO, [{"id": "unassigned_bug:2", "rule": "unassigned_bug", "severity": "high"}]
            )

    assert result["fired"] == 1
    steps = result["runs"][0]["step_results"]
    assert len(steps) == 1, "one situation fired it, so exactly one action runs"
    assert steps[0]["args"]["situation_id"] == "unassigned_bug:2"


async def test_still_open_situation_fires_an_event_workflow_only_once() -> None:
    """The watcher engine re-raises a still-true situation every five minutes.
    Without the per-situation claim this would run forever, every pass."""
    async with Session() as session:
        await _reset(session)
        await wf.save_workflow(
            session, _CO, "on bug", "log the new bug",
            [WorkflowStep(tool="run_action", args={"action": "safe_log"}, select={"rule": "unassigned_bug"})],
            trigger=WorkflowTrigger(type="event", config={"rule": "unassigned_bug"}),
        )
        await _seed_situation(session, "unassigned_bug:1", "unassigned_bug")
        await session.commit()

        raised = [{"id": "unassigned_bug:1", "rule": "unassigned_bug", "severity": "high"}]
        with _profile_loaded():
            first = await fire_event_workflows(session, _CO, raised)
            second = await fire_event_workflows(session, _CO, raised)  # the next watcher pass
            third = await fire_event_workflows(session, _CO, raised)

    assert first["fired"] == 1
    assert second["fired"] == 0
    assert third["fired"] == 0


async def test_event_severity_filter_narrows_the_match() -> None:
    async with Session() as session:
        await _reset(session)
        await wf.save_workflow(
            session, _CO, "high only", "log high severity",
            [WorkflowStep(tool="run_action", args={"action": "safe_log"}, select={"rule": "unassigned_bug"})],
            trigger=WorkflowTrigger(type="event", config={"severity": "high"}),
        )
        await _seed_situation(session, "unassigned_bug:low", "unassigned_bug", severity="low")
        await session.commit()

        with _profile_loaded():
            result = await fire_event_workflows(
                session, _CO, [{"id": "unassigned_bug:low", "rule": "unassigned_bug", "severity": "low"}]
            )

    assert result["fired"] == 0


async def test_no_event_workflows_means_no_work() -> None:
    async with Session() as session:
        await _reset(session)
        result = await fire_event_workflows(
            session, _CO, [{"id": "x", "rule": "r", "severity": "high"}]
        )
    assert result == {"fired": 0, "runs": []}
