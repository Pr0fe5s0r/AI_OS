from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apps.common.analysis import request_action
from apps.common.assistant_tools import _make_dispatch, build_tools
from apps.common.context import approval_policy
from packages.core import workflows as wf
from packages.core.assistant import Dispatch
from packages.core.llm import chat
from packages.core.profile import Profile
from packages.core.situations import list_situations
from packages.shared.schema import (
    Workflow,
    WorkflowPlan,
    WorkflowStep,
    WorkflowStepResult,
    WorkflowTrigger,
)

# Orchestration for saved workflows (Phase 2). Two jobs:
#   plan_workflow() — turn a natural-language goal into an ordered plan of steps,
#     each mapping to a REAL tool the agent already has (build_tools). Ambiguity
#     becomes a clarifying question, not a guess.
#   run_workflow()  — execute a saved plan through the SAME gated dispatcher the
#     agent uses, with one extra brake: "auto-run allowlisted actions only", so
#     a run (especially a scheduled one) never fires an external action that the
#     autonomy allowlist hasn't sanctioned.
#
# The generic mechanics (persistence, step execution) live in core.workflows;
# this module is the profile-aware glue, the same split assistant_tools has with
# core.assistant.


def _param_sig(name: str, spec: dict[str, Any]) -> str:
    """A param's name, showing its allowed values when it has an enum — so the
    planner picks a REAL action name (apply_label), not a plausible-looking
    invention (label) that fails at run time."""
    enum = spec.get("enum") or (spec.get("items") or {}).get("enum")
    return f"{name}={'|'.join(map(str, enum))}" if enum else name


def _tool_catalog(tools: list[dict[str, Any]]) -> str:
    lines = []
    for t in tools:
        fn = t["function"]
        params = (fn.get("parameters", {}) or {}).get("properties", {}) or {}
        sig = ", ".join(_param_sig(n, s) for n, s in params.items()) or "no args"
        lines.append(f"- {fn['name']}({sig}): {fn['description']}")
    return "\n".join(lines)


def _allowed_actions(profile: Profile) -> set[str]:
    return set((profile.moves.get("autonomy") or {}).get("allowed_actions") or [])


def _requires_approval(profile: Profile, step: WorkflowStep) -> bool:
    """Advisory flag for the review UI. The real brake is at run time; this just
    lets a person see, before saving, which steps will pause for them."""
    if step.tool != "run_action":
        return False
    action = str(step.args.get("action", ""))
    registry = profile.moves.get("registry", {}) or {}
    entry = registry.get(action, {})
    return bool(entry.get("approval_required")) or action not in _allowed_actions(profile)


# Which connected app a step touches, and a one-line purpose — the "which app
# for what" that annotates each n8n node. Read tools work over the unified data,
# not one app; run_action's app is the action's auth.source in the registry.
def step_app(profile: Profile, step: WorkflowStep) -> dict[str, str]:
    if step.tool == "run_action":
        action = str(step.args.get("action", ""))
        entry = (profile.moves.get("registry", {}) or {}).get(action, {})
        source = (entry.get("auth") or {}).get("source")
        if source:
            return {"app": source, "does": step.description or f"{action.replace('_', ' ')}"}
        # a 'log' move (draft/note) contacts no external app
        return {"app": "internal", "does": step.description or f"{action.replace('_', ' ')} (prepared, not sent)"}
    reads = {
        "search_events": "reads across your connected tools",
        "list_situations": "reads what needs attention",
        "get_norms": "reads what's normal for you",
        "get_briefing": "reads the workspace summary",
        "describe_system": "reads what the system knows",
    }
    return {"app": "your data", "does": reads.get(step.tool, step.description or step.tool.replace("_", " "))}


def apps_used(profile: Profile, steps: list[WorkflowStep]) -> list[dict[str, str]]:
    """Distinct (app -> what it's used for) across a plan, in first-seen order —
    the 'what apps are used for what' the agent shows when building a workflow."""
    seen: dict[str, str] = {}
    for step in steps:
        info = step_app(profile, step)
        seen.setdefault(info["app"], info["does"])
    return [{"app": app, "does": does} for app, does in seen.items()]


def _parse_trigger(raw: Any) -> WorkflowTrigger:
    if not isinstance(raw, dict):
        return WorkflowTrigger()
    t = str(raw.get("type", "manual"))
    if t not in ("manual", "schedule", "event"):
        t = "manual"
    config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
    return WorkflowTrigger(type=t, config=config or {})


_PLANNER_SYSTEM = (
    "You are a planning agent for an operations system. Turn the user's GOAL into a "
    "TRIGGER plus an ordered list of concrete STEPS, each calling exactly ONE available tool.\n\n"
    "TRIGGER — what starts the workflow:\n"
    '- "manual": the person runs it themselves. config: {}.\n'
    '- "schedule": a recurring time ("every morning", "each Monday"). '
    'config: {"cron": "<5-field cron>", "label": "<the phrase the user used>"}.\n'
    '- "event": something happening ("when a new high-severity issue appears", "whenever an '
    'unassigned bug is raised"). config: {"rule"?: "<situation rule>", "severity"?: "high"}.\n'
    "Infer it from the goal; default to manual when the goal doesn't imply timing or an event.\n\n"
    "STEP rules:\n"
    "- Use ONLY tools from the list below. Never invent a tool or an argument.\n"
    "- 'select' belongs ONLY on a run_action step. Never put it on a read tool.\n"
    "- A run_action MUST target something: either args.situation_id (a REAL id from the open "
    "situations below, for ONE specific item) OR 'select' (a filter, for a group). Never both, "
    "never neither.\n"
    "- For 'each'/'every'/'all <kind>', OR any schedule/event trigger: emit ONE run_action with "
    "'select' like {\"rule\": \"unassigned_bug\"} or {\"severity\": \"high\"} — NOT one step per "
    "item. It fans out to every current match at run time, so it keeps working as items change. "
    "Do NOT enumerate multiple run_action steps for the same action.\n"
    "- A gathering read step (list_situations) is only useful when a LATER step needs its output; "
    "for a pure 'do X to all Y' goal, the single select'd run_action is the whole plan.\n"
    "- If the goal is ambiguous or asks for something no tool can do, add a short question to "
    "'clarifications' rather than guessing.\n"
    "- Keep it minimal: the fewest steps that achieve the goal.\n\n"
    'Output STRICT JSON: {"name": "<=5 word title", "trigger": {"type": str, "config": {}}, '
    '"steps": [{"tool": str, "args": {}, "select": {"rule"?: str, "severity"?: str} | null, '
    '"description": "one plain-language line"}], "clarifications": [str, ...]}'
)


def _plan_from_data(profile: Profile, goal: str, data: dict, tool_names: set[str]) -> WorkflowPlan:
    steps: list[WorkflowStep] = []
    for raw_step in data.get("steps") or []:
        tool = str(raw_step.get("tool", ""))
        if tool not in tool_names:  # drop anything hallucinated outside the catalog
            continue
        select = raw_step.get("select")
        select = select if isinstance(select, dict) and select else None
        args = raw_step.get("args") or {}
        # an action must be able to target something, or it just fails at run time
        # (and, unattended, fails every time) — drop the untargetable step.
        if tool == "run_action" and not select and not args.get("situation_id"):
            continue
        step = WorkflowStep(
            tool=tool,
            args=args,
            description=str(raw_step.get("description", "")),
            select=select if tool == "run_action" else None,  # select is meaningless off run_action
        )
        step.requires_approval = _requires_approval(profile, step)
        steps.append(step)
    return WorkflowPlan(
        goal=goal,
        name=str(data.get("name") or goal[:40]).strip(),
        trigger=_parse_trigger(data.get("trigger")),
        steps=steps,
        clarifications=[str(c) for c in (data.get("clarifications") or [])],
    )


async def plan_workflow(session: AsyncSession, profile: Profile, goal: str) -> WorkflowPlan:
    """Compile a goal into a reviewable plan (trigger + steps). Understands intent
    against what is actually connected — the tool catalog and the real open
    situations — so the plan can only ever propose things the engine can do."""
    tools = build_tools(profile)
    situations = [s for s in await list_situations(session, profile.company_id) if s.status != "resolved"]
    sit_lines = "\n".join(f"- {s.id} [{s.severity}] {s.rule} · {s.title}" for s in situations[:25]) or "(none open)"

    prompt = (
        f"{_PLANNER_SYSTEM}\n\nAVAILABLE TOOLS:\n{_tool_catalog(tools)}\n\n"
        f"OPEN SITUATIONS (id [severity] rule · title):\n{sit_lines}"
    )
    try:
        raw = chat(
            [{"role": "system", "content": prompt}, {"role": "user", "content": f"GOAL: {goal}"}],
            response_format={"type": "json_object"},
        )
        data = json.loads(raw)
    except Exception as exc:  # a provider/parse failure must not 500 the request
        return WorkflowPlan(goal=goal, name=goal[:40], steps=[], clarifications=[f"I couldn't plan this: {exc}"])

    return _plan_from_data(profile, goal, data, {t["function"]["name"] for t in tools})


async def edit_workflow(
    session: AsyncSession, profile: Profile, workflow: Workflow, instruction: str
) -> WorkflowPlan:
    """Re-plan an EXISTING workflow from a natural-language instruction — the
    per-workflow edit chat ("add a step to assign it to me", "change the trigger
    to every morning", "drop the Slack step"). Returns a fresh plan; the caller
    saves it back onto the same workflow. Same catalog + grounding as planning
    from scratch, with the current workflow supplied as the thing to modify."""
    tools = build_tools(profile)
    situations = [s for s in await list_situations(session, profile.company_id) if s.status != "resolved"]
    sit_lines = "\n".join(f"- {s.id} [{s.severity}] {s.rule} · {s.title}" for s in situations[:25]) or "(none open)"
    current = {
        "name": workflow.name,
        "trigger": workflow.trigger.model_dump(),
        "steps": [s.model_dump(exclude={"requires_approval"}) for s in workflow.steps],
    }
    prompt = (
        f"{_PLANNER_SYSTEM}\n\nAVAILABLE TOOLS:\n{_tool_catalog(tools)}\n\n"
        f"OPEN SITUATIONS (id [severity] rule · title):\n{sit_lines}\n\n"
        "You are EDITING an existing workflow. Apply the user's instruction and return the "
        "COMPLETE updated workflow (all steps, not just the change).\n"
        f"CURRENT WORKFLOW:\n{json.dumps(current)}"
    )
    try:
        raw = chat(
            [{"role": "system", "content": prompt}, {"role": "user", "content": f"INSTRUCTION: {instruction}"}],
            response_format={"type": "json_object"},
        )
        data = json.loads(raw)
    except Exception as exc:
        return WorkflowPlan(
            goal=workflow.goal, name=workflow.name, trigger=workflow.trigger,
            steps=workflow.steps, clarifications=[f"I couldn't apply that: {exc}"],
        )

    plan = _plan_from_data(profile, workflow.goal, data, {t["function"]["name"] for t in tools})
    # keep the original name unless the model deliberately renamed it
    if not data.get("name"):
        plan.name = workflow.name
    return plan


def _make_workflow_dispatch(
    session: AsyncSession, profile: Profile, base: Dispatch
) -> Dispatch:
    """Wrap the agent's dispatcher with the allowlist gate. Every tool behaves
    exactly as in chat EXCEPT run_action: an action not on the autonomy
    allowlist is forced into the approval queue instead of running unattended."""
    allowed = _allowed_actions(profile)

    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name != "run_action":
            return await base(name, args)
        action = str(args.get("action", ""))
        if action not in (profile.moves.get("registry") or {}):
            return {"error": f"unknown action {action!r}"}
        on_allowlist = action in allowed
        result = await request_action(
            session, profile, action=action,
            params={"argument": str(args.get("argument", ""))} if args.get("argument") else {},
            situation_id=args.get("situation_id"),
            requested_by="workflow",
            pre_approved=False,
            force_approval=not on_allowlist,  # the gate: off-allowlist ⇒ queue, never fire
        )
        policy = await approval_policy(session, profile)
        return {
            "action": action, "status": result.status, "detail": result.detail,
            "dry_run": policy["dry_run"], "on_allowlist": on_allowlist,
        }

    return dispatch


def _matches_selector(situation: Any, selector: dict[str, Any]) -> bool:
    if "rule" in selector and situation.rule != selector["rule"]:
        return False
    if "severity" in selector and situation.severity != selector["severity"]:
        return False
    return True


async def _expand_steps(
    session: AsyncSession, profile: Profile, steps: list[WorkflowStep]
) -> list[WorkflowStep]:
    """Resolve dynamic (``select``) run_action steps against the situations open
    RIGHT NOW — one concrete step per match. This is what makes a saved or
    scheduled workflow act on today's items instead of the ones frozen into the
    plan when it was written. A selector that matches nothing contributes no
    steps (there is simply nothing to do), which is the correct quiet resting
    state, not a failure."""
    if not any(s.select and s.enabled for s in steps):
        return steps  # nothing dynamic to resolve — skip the situations query entirely
    open_sits = [s for s in await list_situations(session, profile.company_id) if s.status != "resolved"]
    expanded: list[WorkflowStep] = []
    for step in steps:
        if not step.enabled or not step.select:
            expanded.append(step)  # disabled or non-dynamic: execute_plan handles it
            continue
        for sit in open_sits:
            if _matches_selector(sit, step.select):
                expanded.append(
                    WorkflowStep(
                        tool=step.tool,
                        args={**step.args, "situation_id": sit.id},
                        description=step.description,
                        requires_approval=step.requires_approval,
                    )
                )
    return expanded


def _summarize(results: list[WorkflowStepResult]) -> str:
    """A short, deterministic plain-language receipt — no extra LLM call."""
    if not results:
        return "No steps ran."
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    phrasing = {
        "done": "completed", "executed": "executed", "dry_run": "rehearsed",
        "pending_approval": "queued for approval", "recorded": "recorded", "failed": "failed",
    }
    parts = [f"{n} {phrasing.get(status, status)}" for status, n in counts.items()]
    return f"Ran {len(results)} step{'s' if len(results) != 1 else ''}: " + ", ".join(parts) + "."


def _run_status(results: list[WorkflowStepResult]) -> str:
    if any(r.status == "pending_approval" for r in results):
        return "needs_approval"
    if results and all(r.status == "failed" for r in results):
        return "failed"
    return "done"


async def run_workflow(
    session: AsyncSession, profile: Profile, workflow_id: int, trigger: str = "manual"
) -> dict[str, Any]:
    """Execute a saved workflow now. Returns the run id, final status, per-step
    results and a summary. External actions ride act()'s approval/dry-run brake
    plus the allowlist gate above."""
    workflow = await wf.get_workflow(session, profile.company_id, workflow_id)
    if workflow is None:
        return {"error": "workflow not found"}

    run_id = await wf.create_run(session, workflow_id, profile.company_id, trigger)
    steps = await _expand_steps(session, profile, workflow.steps)
    base = _make_dispatch(session, profile)
    dispatch = _make_workflow_dispatch(session, profile, base)
    results = await wf.execute_plan(steps, dispatch)
    status = _run_status(results)
    summary = _summarize(results)
    await wf.finish_run(session, run_id, status, results, summary)
    await session.commit()
    return {
        "run_id": run_id,
        "status": status,
        "summary": summary,
        "step_results": [r.model_dump() for r in results],
    }
