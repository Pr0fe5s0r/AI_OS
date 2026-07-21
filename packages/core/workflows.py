from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.shared.schema import (
    Workflow,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepResult,
    WorkflowTrigger,
)

# Persistence + the generic step executor for saved agentic workflows (Phase 2).
#
# Like packages.core.assistant (the tool-use loop), THIS module holds no opinion
# about what any step does: execute_plan() takes the steps and a `dispatch`
# callable as ARGUMENTS and runs them. The dispatch — which knows the profile's
# real tools and rides act()'s approval/dry-run brake — is supplied by the
# apps.common orchestration layer. The engine never touches an external system.

# Same shape as assistant.Dispatch: (tool_name, args) -> JSON-serializable result.
Dispatch = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

# An optional progress hook, awaited before and after each step. Part B (SSE
# streaming) passes one; Part A leaves it None. Kept here so streaming is a
# caller concern, not a rewrite of the executor.
StepEvent = Callable[[str, int, WorkflowStep, WorkflowStepResult | None], Awaitable[None]]


# ------------------------------ persistence ------------------------------


_COLS = (
    "id, company_id, name, goal, steps, trigger, enabled, "
    "created_by, created_at, updated_at, last_run_at"
)


def _row_to_workflow(row: Any) -> Workflow:
    steps = row.steps if isinstance(row.steps, list) else json.loads(row.steps)
    trigger = row.trigger if isinstance(row.trigger, dict) else json.loads(row.trigger)
    return Workflow(
        id=row.id,
        company_id=row.company_id,
        name=row.name,
        goal=row.goal,
        steps=[WorkflowStep(**s) for s in steps],
        trigger=WorkflowTrigger(**(trigger or {})),
        enabled=row.enabled,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_run_at=row.last_run_at,
    )


async def save_workflow(
    session: AsyncSession,
    company_id: str,
    name: str,
    goal: str,
    steps: list[WorkflowStep],
    trigger: WorkflowTrigger | None = None,
    created_by: str = "ui",
) -> Workflow:
    row = (
        await session.execute(
            text(
                f"""
                INSERT INTO workflows (company_id, name, goal, steps, trigger, created_by)
                VALUES (:c, :name, :goal, CAST(:steps AS jsonb), CAST(:trigger AS jsonb), :by)
                RETURNING {_COLS}
                """
            ),
            {
                "c": company_id,
                "name": name,
                "goal": goal,
                "steps": json.dumps([s.model_dump() for s in steps]),
                "trigger": json.dumps((trigger or WorkflowTrigger()).model_dump()),
                "by": created_by,
            },
        )
    ).one()
    return _row_to_workflow(row)


async def list_workflows(session: AsyncSession, company_id: str) -> list[Workflow]:
    rows = await session.execute(
        text(f"SELECT {_COLS} FROM workflows WHERE company_id = :c ORDER BY updated_at DESC, id DESC"),
        {"c": company_id},
    )
    return [_row_to_workflow(r) for r in rows]


async def get_workflow(
    session: AsyncSession, company_id: str, workflow_id: int
) -> Workflow | None:
    row = (
        await session.execute(
            text(f"SELECT {_COLS} FROM workflows WHERE company_id = :c AND id = :id"),
            {"c": company_id, "id": workflow_id},
        )
    ).first()
    return _row_to_workflow(row) if row is not None else None


async def update_workflow(
    session: AsyncSession, company_id: str, workflow_id: int, **changes: Any
) -> Workflow | None:
    """Patch a workflow. Only a closed set of columns is patchable, so a caller
    (or a model) can never write an arbitrary field."""
    allowed = {"name", "goal", "steps", "trigger", "enabled"}
    patch = {k: v for k, v in changes.items() if k in allowed and v is not None}
    if not patch:
        return await get_workflow(session, company_id, workflow_id)
    sets = []
    params: dict[str, Any] = {"c": company_id, "id": workflow_id}
    for key, value in patch.items():
        if key == "steps":
            sets.append("steps = CAST(:steps AS jsonb)")
            params["steps"] = json.dumps(
                [s.model_dump() if isinstance(s, WorkflowStep) else s for s in value]
            )
        elif key == "trigger":
            sets.append("trigger = CAST(:trigger AS jsonb)")
            params["trigger"] = json.dumps(
                value.model_dump() if isinstance(value, WorkflowTrigger) else value
            )
        else:
            sets.append(f"{key} = :{key}")
            params[key] = value
    sets.append("updated_at = now()")
    await session.execute(
        text(f"UPDATE workflows SET {', '.join(sets)} WHERE company_id = :c AND id = :id"),
        params,
    )
    return await get_workflow(session, company_id, workflow_id)


async def delete_workflow(session: AsyncSession, company_id: str, workflow_id: int) -> bool:
    result = await session.execute(
        text("DELETE FROM workflows WHERE company_id = :c AND id = :id"),
        {"c": company_id, "id": workflow_id},
    )
    return bool(cast(CursorResult, result).rowcount)


# ------------------------------- runs -------------------------------


async def create_run(
    session: AsyncSession, workflow_id: int, company_id: str, trigger: str = "manual"
) -> int:
    row = (
        await session.execute(
            text(
                """
                INSERT INTO workflow_runs (workflow_id, company_id, status, trigger)
                VALUES (:wid, :c, 'running', :trigger) RETURNING id
                """
            ),
            {"wid": workflow_id, "c": company_id, "trigger": trigger},
        )
    ).one()
    return int(row.id)


async def finish_run(
    session: AsyncSession,
    run_id: int,
    status: str,
    step_results: list[WorkflowStepResult],
    summary: str = "",
) -> None:
    await session.execute(
        text(
            """
            UPDATE workflow_runs
            SET status = :st, step_results = CAST(:sr AS jsonb), summary = :sum,
                finished_at = now()
            WHERE id = :id
            """
        ),
        {
            "id": run_id,
            "st": status,
            "sr": json.dumps([r.model_dump() for r in step_results]),
            "sum": summary,
        },
    )
    await session.execute(
        text(
            """
            UPDATE workflows SET last_run_at = now()
            WHERE id = (SELECT workflow_id FROM workflow_runs WHERE id = :id)
            """
        ),
        {"id": run_id},
    )


async def list_runs(
    session: AsyncSession, company_id: str, workflow_id: int, limit: int = 20
) -> list[WorkflowRun]:
    rows = await session.execute(
        text(
            """
            SELECT id, workflow_id, company_id, status, trigger, step_results,
                   summary, started_at, finished_at
            FROM workflow_runs
            WHERE company_id = :c AND workflow_id = :wid
            ORDER BY started_at DESC LIMIT :l
            """
        ),
        {"c": company_id, "wid": workflow_id, "l": limit},
    )
    out: list[WorkflowRun] = []
    for r in rows:
        results = r.step_results if isinstance(r.step_results, list) else json.loads(r.step_results)
        out.append(
            WorkflowRun(
                id=r.id,
                workflow_id=r.workflow_id,
                company_id=r.company_id,
                status=r.status,
                trigger=r.trigger,
                step_results=[WorkflowStepResult(**s) for s in results],
                summary=r.summary,
                started_at=r.started_at,
                finished_at=r.finished_at,
            )
        )
    return out


# --------------------------- the generic executor ---------------------------


def _status_of(result: dict[str, Any]) -> tuple[str, str]:
    """Normalize a dispatch result into (status, detail). Action steps already
    carry act()'s status vocabulary; read/control steps get done/failed."""
    if result.get("error"):
        return "failed", str(result["error"])
    if "status" in result:  # a run_action result
        return str(result["status"]), str(result.get("detail", ""))
    if "effect" in result:  # a control tool (set_*, reset_measurement)
        return "done", str(result["effect"])
    return "done", ""


async def execute_plan(
    steps: list[WorkflowStep],
    dispatch: Dispatch,
    *,
    on_event: StepEvent | None = None,
) -> list[WorkflowStepResult]:
    """Run a plan's steps in order through ``dispatch``, collecting an honest
    per-step receipt. Purely mechanical — no profile knowledge, no external
    calls of its own; the dispatch owns the approval/dry-run gate.

    A failing step does not abort the run: later steps may not depend on it,
    and a half-finished run with a clear record beats an opaque hard stop.
    ``on_event`` (if given) is awaited before and after each step, so a caller
    can stream progress without changing this loop.
    """
    results: list[WorkflowStepResult] = []
    for i, step in enumerate(steps):
        if not step.enabled:
            # kept in the graph, deliberately turned off — record it, don't run it
            result = WorkflowStepResult(
                tool=step.tool, args=step.args, status="skipped", detail="step is disabled"
            )
            results.append(result)
            if on_event is not None:
                await on_event("step_done", i, step, result)
            continue
        if on_event is not None:
            await on_event("step_start", i, step, None)
        try:
            raw = await dispatch(step.tool, step.args)
        except Exception as exc:  # a broken tool must not kill the run
            raw = {"error": f"{step.tool} failed: {exc}"}
        status, detail = _status_of(raw)
        result = WorkflowStepResult(
            tool=step.tool, args=step.args, status=status, detail=detail, result=raw
        )
        results.append(result)
        if on_event is not None:
            await on_event("step_done", i, step, result)
    return results
