from __future__ import annotations

import json

from sqlalchemy import text

import apps.common.workflows_flow as wff
from apps.common.workflows_flow import apps_used, run_workflow, step_app
from packages.core import workflows as wf
from packages.core.db import Session
from packages.core.profile import Profile
from packages.shared.schema import WorkflowStep, WorkflowTrigger

# Phase 2: saved agentic workflows. Three layers, tested separately:
#   1. the generic executor (core.workflows.execute_plan) — pure mechanics
#   2. the planner (goal -> steps, with a scripted fake model)
#   3. the allowlist gate — the safety-critical bit: a run_action step whose
#      action is NOT on the autonomy allowlist must queue, never fire unattended.

_CO = "test-workflows"


def _profile() -> Profile:
    """Minimal profile with two log actions — one on the autonomy allowlist,
    one not — so the gate's effect is isolated from approval_required."""
    return Profile(
        company_id=_CO,
        sources=[], things={}, links={}, rhythms=[], watchers=[], vocabulary={},
        moves={
            "registry": {
                # placeholder-free templates: these tests exercise the allowlist
                # gate, not param templating (which needs a real situation row)
                "safe_log": {"kind": "log", "approval_required": False, "template": "noted (safe)"},
                "gated_log": {"kind": "log", "approval_required": False, "template": "noted (gated)"},
            },
            "autonomy": {"allowed_actions": ["safe_log"]},
            "approval_defaults": {"dry_run": True},
        },
    )


# ------------------------------ the executor ------------------------------


async def test_execute_plan_runs_in_order_and_survives_a_failure() -> None:
    seen: list[str] = []

    async def dispatch(name, args):
        seen.append(name)
        if name == "boom":
            raise RuntimeError("kaboom")
        return {"status": "done"} if name == "a" else {"ok": True}

    steps = [WorkflowStep(tool="a"), WorkflowStep(tool="boom"), WorkflowStep(tool="c")]
    results = await wf.execute_plan(steps, dispatch)

    assert seen == ["a", "boom", "c"]  # a mid-plan failure does not abort the rest
    assert results[0].status == "done"
    assert results[1].status == "failed"
    assert "kaboom" in results[1].detail
    assert results[2].status == "done"


async def test_disabled_step_is_skipped_not_run() -> None:
    ran: list[str] = []

    async def dispatch(name, args):
        ran.append(name)
        return {"ok": True}

    steps = [
        WorkflowStep(tool="a"),
        WorkflowStep(tool="b", enabled=False),
        WorkflowStep(tool="c"),
    ]
    results = await wf.execute_plan(steps, dispatch)

    assert ran == ["a", "c"]  # the disabled node never dispatched
    assert results[1].status == "skipped"
    assert [r.tool for r in results] == ["a", "b", "c"]  # but still recorded, in order


async def test_execute_plan_emits_progress_events() -> None:
    events: list[tuple[str, int]] = []

    async def dispatch(name, args):
        return {"ok": True}

    async def on_event(kind, i, step, result):
        events.append((kind, i))

    await wf.execute_plan([WorkflowStep(tool="x")], dispatch, on_event=on_event)
    assert events == [("step_start", 0), ("step_done", 0)]


# ------------------------------- the planner -------------------------------


def _fake_chat(payload: dict):
    def chat(messages, **kwargs):
        return json.dumps(payload)

    return chat


async def test_planner_keeps_real_tools_and_drops_hallucinations(monkeypatch) -> None:
    payload = {
        "name": "Label stale bugs",
        "steps": [
            {"tool": "list_situations", "args": {}, "description": "see what's open"},
            {"tool": "run_action", "args": {"action": "safe_log", "situation_id": "sit-1"}, "description": "log it"},
            {"tool": "totally_made_up", "args": {}, "description": "nope"},
        ],
        "clarifications": [],
    }
    monkeypatch.setattr(wff, "chat", _fake_chat(payload))

    async with Session() as session:
        plan = await wff.plan_workflow(session, _profile(), "label stale bugs")

    tools = [s.tool for s in plan.steps]
    assert "list_situations" in tools
    assert "run_action" in tools
    assert "totally_made_up" not in tools  # not in the catalog -> dropped, never saved
    assert plan.name == "Label stale bugs"


async def test_planner_surfaces_clarifications(monkeypatch) -> None:
    payload = {"name": "Unclear", "steps": [], "clarifications": ["Which repo did you mean?"]}
    monkeypatch.setattr(wff, "chat", _fake_chat(payload))

    async with Session() as session:
        plan = await wff.plan_workflow(session, _profile(), "do the thing")

    assert plan.steps == []
    assert plan.clarifications == ["Which repo did you mean?"]


async def test_planner_infers_a_schedule_trigger(monkeypatch) -> None:
    payload = {
        "name": "Morning triage",
        "trigger": {"type": "schedule", "config": {"cron": "0 9 * * *", "label": "every morning"}},
        "steps": [{"tool": "list_situations", "args": {}}],
        "clarifications": [],
    }
    monkeypatch.setattr(wff, "chat", _fake_chat(payload))
    async with Session() as session:
        plan = await wff.plan_workflow(session, _profile(), "every morning, triage what's open")
    assert plan.trigger.type == "schedule"
    assert plan.trigger.config["cron"] == "0 9 * * *"


async def test_planner_defaults_trigger_to_manual(monkeypatch) -> None:
    payload = {"name": "One-off", "steps": [{"tool": "list_situations", "args": {}}], "clarifications": []}
    monkeypatch.setattr(wff, "chat", _fake_chat(payload))
    async with Session() as session:
        plan = await wff.plan_workflow(session, _profile(), "show me what's open")
    assert plan.trigger.type == "manual"


async def test_edit_workflow_returns_the_full_updated_plan(monkeypatch) -> None:
    async with Session() as session:
        await _reset(session)
        workflow = await wf.save_workflow(
            session, _CO, "orig", "check open", [WorkflowStep(tool="list_situations")]
        )
        await session.commit()

        payload = {
            "name": "orig",
            "trigger": {"type": "manual", "config": {}},
            "steps": [{"tool": "list_situations", "args": {}}, {"tool": "get_norms", "args": {}}],
            "clarifications": [],
        }
        monkeypatch.setattr(wff, "chat", _fake_chat(payload))
        plan = await wff.edit_workflow(session, _profile(), workflow, "also check the norms")

    assert [s.tool for s in plan.steps] == ["list_situations", "get_norms"]


# ------------------------------ app annotations ------------------------------


def test_step_app_names_the_source_for_actions() -> None:
    profile = Profile(
        company_id=_CO, sources=[], things={}, links={}, rhythms=[], watchers=[], vocabulary={},
        moves={"registry": {"apply_label": {"kind": "http", "auth": {"source": "github"}}}},
    )
    label_step = WorkflowStep(tool="run_action", args={"action": "apply_label"})
    read_step = WorkflowStep(tool="list_situations")
    assert step_app(profile, label_step)["app"] == "github"
    assert step_app(profile, read_step)["app"] == "your data"


def test_apps_used_lists_each_app_once() -> None:
    profile = Profile(
        company_id=_CO, sources=[], things={}, links={}, rhythms=[], watchers=[], vocabulary={},
        moves={"registry": {
            "apply_label": {"kind": "http", "auth": {"source": "github"}},
            "note_it": {"kind": "log"},
        }},
    )
    steps = [
        WorkflowStep(tool="list_situations"),
        WorkflowStep(tool="run_action", args={"action": "apply_label"}),
        WorkflowStep(tool="run_action", args={"action": "apply_label"}),  # same app again
        WorkflowStep(tool="run_action", args={"action": "note_it"}),
    ]
    apps = [a["app"] for a in apps_used(profile, steps)]
    assert apps.count("github") == 1  # deduped
    assert "your data" in apps and "internal" in apps


# ------------------------- persistence + the gate -------------------------


async def _reset(session) -> None:
    await session.execute(text("DELETE FROM workflow_runs WHERE company_id = :c"), {"c": _CO})
    await session.execute(text("DELETE FROM workflows WHERE company_id = :c"), {"c": _CO})
    await session.execute(text("DELETE FROM actions WHERE company_id = :c"), {"c": _CO})
    await session.commit()


async def test_save_list_get_delete_roundtrip() -> None:
    async with Session() as session:
        await _reset(session)
        steps = [WorkflowStep(tool="list_situations", description="look")]
        saved = await wf.save_workflow(session, _CO, "My flow", "check what's open", steps)
        await session.commit()
        assert saved.id is not None

        listed = await wf.list_workflows(session, _CO)
        assert any(w.id == saved.id for w in listed)

        got = await wf.get_workflow(session, _CO, saved.id)
        assert got is not None and got.name == "My flow" and got.steps[0].tool == "list_situations"

        await wf.update_workflow(session, _CO, saved.id, enabled=False)
        await session.commit()
        assert (await wf.get_workflow(session, _CO, saved.id)).enabled is False

        assert await wf.delete_workflow(session, _CO, saved.id) is True
        assert await wf.get_workflow(session, _CO, saved.id) is None


async def test_trigger_persists_through_save_and_get() -> None:
    async with Session() as session:
        await _reset(session)
        trig = WorkflowTrigger(type="event", config={"rule": "unassigned_bug"})
        saved = await wf.save_workflow(
            session, _CO, "on new bug", "label new bugs", [WorkflowStep(tool="list_situations")], trigger=trig
        )
        await session.commit()
        got = await wf.get_workflow(session, _CO, saved.id)

    assert got is not None
    assert got.trigger.type == "event"
    assert got.trigger.config["rule"] == "unassigned_bug"


async def test_allowlist_gate_queues_off_allowlist_action() -> None:
    """The safety-critical path. A workflow run auto-executes an allowlisted
    action but FORCES an off-allowlist one into the approval queue — it never
    fires unattended, even though nothing marked it approval_required."""
    async with Session() as session:
        await _reset(session)
        steps = [
            WorkflowStep(tool="run_action", args={"action": "safe_log", "situation_id": "sit-1"}),
            WorkflowStep(tool="run_action", args={"action": "gated_log", "situation_id": "sit-2"}),
        ]
        workflow = await wf.save_workflow(session, _CO, "gate test", "do both", steps)
        await session.commit()

        result = await run_workflow(session, _profile(), workflow.id, trigger="manual")

    statuses = [r["status"] for r in result["step_results"]]
    assert statuses[0] == "recorded"          # on the allowlist -> ran (a log contacts nobody)
    assert statuses[1] == "pending_approval"  # off the allowlist -> forced to queue
    assert result["status"] == "needs_approval"


async def _seed_situation(session, sid: str, rule: str, severity: str = "high") -> None:
    await session.execute(
        text(
            """
            INSERT INTO situations
                (id, company_id, rule, severity, title, summary, recommended_action,
                 evidence, status, created_at, kind, choices)
            VALUES (:id, :c, :rule, :sev, :title, '', NULL, '[]'::jsonb, 'open', now(), 'business', NULL)
            ON CONFLICT (id) DO UPDATE SET status = 'open', rule = EXCLUDED.rule, severity = EXCLUDED.severity
            """
        ),
        {"id": sid, "c": _CO, "rule": rule, "sev": severity, "title": f"{rule} situation"},
    )


async def test_selector_step_fans_out_one_action_per_match() -> None:
    """A.5 dynamic targeting: a run_action with a `select` resolves against the
    situations open at RUN time — one concrete action per match, others ignored."""
    async with Session() as session:
        await _reset(session)
        await session.execute(text("DELETE FROM situations WHERE company_id = :c"), {"c": _CO})
        await _seed_situation(session, "unassigned_bug:1", "unassigned_bug")
        await _seed_situation(session, "unassigned_bug:2", "unassigned_bug")
        await _seed_situation(session, "broken_rhythm:x", "broken_rhythm")
        await session.commit()

        steps = [WorkflowStep(tool="run_action", args={"action": "safe_log"}, select={"rule": "unassigned_bug"})]
        workflow = await wf.save_workflow(session, _CO, "fan out", "log every unassigned bug", steps)
        await session.commit()

        result = await run_workflow(session, _profile(), workflow.id)

    # two matching situations -> two concrete steps; the broken_rhythm one is not touched
    assert len(result["step_results"]) == 2
    assert all(r["status"] == "recorded" for r in result["step_results"])


async def test_selector_matching_nothing_does_nothing() -> None:
    async with Session() as session:
        await _reset(session)
        await session.execute(text("DELETE FROM situations WHERE company_id = :c"), {"c": _CO})
        await _seed_situation(session, "broken_rhythm:x", "broken_rhythm")
        await session.commit()

        steps = [WorkflowStep(tool="run_action", args={"action": "safe_log"}, select={"rule": "unassigned_bug"})]
        workflow = await wf.save_workflow(session, _CO, "empty fan out", "log unassigned bugs", steps)
        await session.commit()

        result = await run_workflow(session, _profile(), workflow.id)

    # nothing matched -> no action ran; a quiet resting state, not a failure
    assert result["step_results"] == []
    assert result["status"] == "done"


async def test_run_is_recorded_with_results() -> None:
    async with Session() as session:
        await _reset(session)
        steps = [WorkflowStep(tool="run_action", args={"action": "safe_log", "situation_id": "s"})]
        workflow = await wf.save_workflow(session, _CO, "record test", "log one", steps)
        await session.commit()

        await run_workflow(session, _profile(), workflow.id)
        runs = await wf.list_runs(session, _CO, workflow.id)

    assert len(runs) == 1
    assert runs[0].status == "done"
    assert runs[0].step_results[0].tool == "run_action"
    assert "1 recorded" in runs[0].summary
