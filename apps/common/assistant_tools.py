from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apps.common.analysis import request_action
from apps.common.context import DRY_RUN_KEY, approval_policy
from apps.common.understanding import describe
from packages.core import audit
from packages.core.assistant import run_tool_loop
from packages.core.briefing import build_briefing
from packages.core.norms import get_norms, reset_norms
from packages.core.profile import Profile, set_action_approval, set_autonomy
from packages.core.search import search
from packages.core.settings import set_setting
from packages.core.situations import list_situations

# The agent's toolbox (checkpoint 4). The generic loop lives in
# packages.core.assistant; THIS module is orchestration — it declares the
# tools (as data) and wires each to a real core function, loading the profile
# so the model can act on the actual connected company.
#
# The one write tool, run_action, goes through the SAME approval brake as the
# Feed's buttons (request_action -> act()): in Practice mode it rehearses
# (dry_run), and any action the profile marks approval_required lands in the
# pending-approval queue. The model can decide to DO things; it can never
# bypass the human gate or touch an external system directly.


def humanize_metric(metric: str) -> str:
    """`issue_resolution_hours` -> `issue resolution`. The unit is reported
    separately, so repeating it in the name reads clumsily."""
    words = metric.replace("_", " ")
    for unit in (" hours", " minutes", " days", " per hour"):
        if words.endswith(unit):
            return words[: -len(unit)]
    return words


def _read_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "search_events",
                "description": "Search the company's connected events (issues, PRs, tickets, messages) by meaning and keyword. Use this to answer questions about what's happening.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "what to look for"},
                        "limit": {"type": "integer", "description": "max results (default 5)"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_situations",
                "description": "List the currently open situations (risks/flags the watcher engine has raised). Use this to see what needs attention and to get situation ids for taking action.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_briefing",
                "description": "Get the high-level workspace summary: event counts, how many situations are open, how many actions await approval.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_norms",
                "description": "Get the learned baselines (e.g. typical issue-resolution time) with their maturity and sample count.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


def _control_tools(profile: Profile) -> list[dict[str, Any]]:
    """Tools that CHANGE the system, not just read it.

    This is what makes the chat the control surface for the whole product:
    anything a settings screen would offer, you can just ask for. Every one
    of these writes a new profile version or a setting and lands in the audit
    log, so "I told it to stop asking me" is a recorded decision, not a
    mystery someone discovers three weeks later.
    """
    action_names = sorted(profile.moves.get("registry", {}) or {})
    return [
        {
            "type": "function",
            "function": {
                "name": "set_practice_mode",
                "description": (
                    "Turn Practice mode on or off. ON = nothing is ever sent to real "
                    "systems (safe rehearsal). OFF = approved actions really change the "
                    "user's tools. Only turn it OFF when the user clearly asks to go live."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"on": {"type": "boolean"}},
                    "required": ["on"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_action_approval",
                "description": (
                    "Change whether one action waits for human approval. Use this when the "
                    "user says things like 'stop asking me before labelling' (required=false) "
                    "or 'always check with me before commenting' (required=true)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": action_names},
                        "required": {"type": "boolean"},
                    },
                    "required": ["action", "required"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_autonomy",
                "description": (
                    "Tune how independently the AI works. allowed_actions = the moves it may "
                    "take alone. min_confidence = how sure it must be (0-1; lower means bolder). "
                    "escalate_severities = severities that ALWAYS reach a human."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "allowed_actions": {"type": "array", "items": {"type": "string", "enum": action_names}},
                        "min_confidence": {"type": "number"},
                        "escalate_severities": {"type": "array", "items": {"type": "string"}},
                        "enabled": {"type": "boolean"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "reset_measurement",
                "description": (
                    "Recalculate a learned baseline from a date onward, ignoring everything "
                    "before it. Use when the user says a measurement is wrong because "
                    "something changed — 'we reorganised in March', 'those old tickets don't "
                    "count'. The cutoff is remembered, so future recalculations respect it too."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string", "description": "which measurement, e.g. issue_resolution_hours"},
                        "from_date": {"type": "string", "description": "ISO date; ignore anything that started before this"},
                    },
                    "required": ["metric", "from_date"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "describe_system",
                "description": (
                    "Everything you currently know about this company: what you watch, what "
                    "you learned, what you check for, what you may do, and whether Practice "
                    "mode is on. Use this for questions like 'what do you know about us?', "
                    "'what are you watching?', 'how independent are you?'."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


def _action_tool(profile: Profile) -> dict[str, Any]:
    """The write tool. Its action enum is the profile's registry — the model
    can only ever pick a move the company has actually declared."""
    registry = profile.moves.get("registry", {}) or {}
    action_names = sorted(registry)
    return {
        "type": "function",
        "function": {
            "name": "run_action",
            "description": (
                "Take an action on a situation (e.g. label it, assign it, escalate it). "
                "Goes through the human-approval brake: risky actions queue for approval, "
                "and in Practice mode nothing is sent externally. Always pass the situation_id "
                "from list_situations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": action_names, "description": "which registered move to run"},
                    "situation_id": {"type": "string", "description": "the situation this action addresses"},
                    "argument": {"type": "string", "description": "the action's argument, e.g. a label name or an assignee username"},
                },
                "required": ["action", "situation_id"],
            },
        },
    }


def build_tools(profile: Profile) -> list[dict[str, Any]]:
    return [*_read_tools(), _action_tool(profile), *_control_tools(profile)]


async def _system_prompt(session: AsyncSession, profile: Profile) -> str:
    vocab = profile.vocabulary.get("terms", {}) or {}
    thing_word = vocab.get("thing", "item")
    situations = await list_situations(session, profile.company_id)
    open_sits = [s for s in situations if s.status != "resolved"]
    registry = profile.moves.get("registry", {}) or {}

    sit_lines = "\n".join(
        f"- {s.id} [{s.severity}] {s.title}" for s in open_sits[:20]
    ) or "(none open right now)"
    action_lines = "\n".join(
        f"- {name}: {entry.get('argument', 'no argument needed')}"
        for name, entry in registry.items()
    ) or "(no actions configured)"

    return (
        f"You are the operations agent for a company (its work is called \"{thing_word}s\"). "
        "You run this system. The user should be able to do ANYTHING by talking to you — "
        "ask about the data, take action on it, or change how the system itself behaves.\n\n"
        "Use the tools to ground every answer in the company's REAL data — never invent "
        "events, numbers, or situation ids. If a search returns nothing, say so plainly.\n\n"
        "TAKING ACTION: when the user asks you to do something, call run_action with the "
        "right situation_id from list_situations. Do not ask permission first — the system "
        "already decides what needs approval, and it will tell you what happened.\n\n"
        "CHANGING THE SYSTEM: you can change the rules themselves. 'stop asking me before "
        "labelling' -> set_action_approval. 'be more independent' -> set_autonomy. 'go live' "
        "/ 'stop touching my real tools' -> set_practice_mode. 'what do you know about us?' "
        "-> describe_system. After changing something, say plainly what will now be different.\n\n"
        "HOW TO WRITE. You are read on a narrow screen by a busy person.\n"
        "- Lead with the answer in one short sentence.\n"
        "- Then at most 3-4 bullets, one line each, starting with '- '.\n"
        "- Bold only the few words that carry the number or the verdict.\n"
        "- Never write a paragraph longer than two lines. Never dump raw field "
        "names, ids or statistics the tool did not phrase for you.\n"
        "- If a tool says a measurement is 'still learning', say that in words "
        "instead of quoting internal numbers.\n\n"
        "ABSOLUTE RULE — NEVER CLAIM AN UNTAKEN ACTION.\n"
        "You have no memory and no hands outside these tools. If you did not call a tool in "
        "THIS message, then nothing happened, and you must not say otherwise. Writing "
        "\"I've updated the system\" without having called the tool is a lie that the user "
        "will act on. When a request needs a tool, CALL IT FIRST and describe only what the "
        "tool actually returned. If you cannot do something, say so plainly.\n\n"
        f"Currently open situations:\n{sit_lines}\n\n"
        f"Actions you may run (name: what its argument means):\n{action_lines}"
    )


def _make_dispatch(session: AsyncSession, profile: Profile):
    company_id = profile.company_id

    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "search_events":
            limit = int(args.get("limit") or 5)
            results = await search(session, company_id, str(args.get("query", "")), limit=min(limit, 10))
            return {
                "count": len(results),
                "results": [
                    {
                        "id": r["id"], "source": r["source"], "type": r["type"],
                        "timestamp": r["timestamp"], "excerpt": (r["content"] or "")[:240],
                        "url": (r.get("metadata") or {}).get("url"),
                    }
                    for r in results
                ],
            }
        if name == "list_situations":
            situations = await list_situations(session, company_id)
            return {
                "situations": [
                    {"id": s.id, "severity": s.severity, "title": s.title, "summary": s.summary, "status": s.status}
                    for s in situations if s.status != "resolved"
                ]
            }
        if name == "get_briefing":
            return await build_briefing(session, company_id, profile.vocabulary.get("briefing_policy", []))
        if name == "get_norms":
            # Shaped for a HUMAN answer, not a stats dump. Handing the model
            # raw mean/std/trend_per_period got it reporting things like
            # "the mean is -1.1" — a trend-extrapolated internal number that
            # is meaningless to a person and reads as a bug. Give it only
            # what it should ever say out loud.
            norms = await get_norms(session, company_id, scope="business")
            rhythms_by_name = {r["name"]: r for r in profile.rhythms if r.get("name")}
            out = []
            for n in norms:
                if n.n == 0:
                    if n.maturity == "unmeasurable":
                        # structurally missing, not just early — never say "wait",
                        # that tells the person to do nothing when they should fix
                        # the profile or connect the system that has this field.
                        rhythm = rhythms_by_name.get(n.metric, {})
                        field = rhythm.get("end_field", "that field")
                        out.append({
                            "what": humanize_metric(n.metric),
                            "known": False,
                            "why": (
                                f"we cannot measure this — `{field}` has never appeared on any "
                                f"{rhythm.get('type', 'record')} we've read, so either the source "
                                "doesn't send it or the profile is pointing at the wrong field"
                            ),
                        })
                    else:
                        out.append({
                            "what": humanize_metric(n.metric),
                            "known": False,
                            "why": "nothing has finished yet, so there is nothing to measure",
                        })
                    continue
                out.append({
                    "what": humanize_metric(n.metric),
                    "known": True,
                    "typically": f"{n.median} {n.unit}",
                    "based_on": f"{n.n} finished {'example' if n.n == 1 else 'examples'}",
                    "how_sure": {
                        "insufficient": "not enough data to rely on",
                        "learning": "still learning, treat as a rough guide",
                        "stable": "dependable",
                    }.get(n.maturity, n.maturity),
                    "direction": (
                        "getting slower" if n.trend_per_period > 0.01
                        else "getting faster" if n.trend_per_period < -0.01
                        else "steady"
                    ),
                })
            return {"measurements": out}
        if name == "set_practice_mode":
            on = bool(args.get("on"))
            await set_setting(session, company_id, DRY_RUN_KEY, on)
            await audit.record(
                session, company_id, "agent", "settings.dry_run",
                target="practice_mode" if on else "live_writes", metadata={"dry_run": on},
            )
            await session.commit()
            return {
                "practice_mode": on,
                "effect": "nothing will be sent to real systems"
                if on else "approved actions now really change connected tools",
            }
        if name == "set_action_approval":
            action = str(args.get("action", ""))
            required = bool(args.get("required"))
            updated = await set_action_approval(session, company_id, action, required)
            if updated is None:
                return {"error": f"no registered action {action!r}"}
            await audit.record(
                session, company_id, "agent", "policy.approval_changed",
                target=action, metadata={"approval_required": required, "profile_version": updated.version},
            )
            await session.commit()
            return {
                "action": action, "approval_required": required,
                "profile_version": updated.version,
                "effect": f"{action} will now {'wait for you' if required else 'run on its own'}",
            }
        if name == "set_autonomy":
            updated = await set_autonomy(
                session, company_id,
                allowed_actions=args.get("allowed_actions"),
                min_confidence=args.get("min_confidence"),
                escalate_severities=args.get("escalate_severities"),
                enabled=args.get("enabled"),
            )
            if updated is None:
                return {"error": "nothing valid to change"}
            autonomy = updated.moves.get("autonomy", {})
            await audit.record(
                session, company_id, "agent", "policy.autonomy_changed",
                metadata={"autonomy": autonomy, "profile_version": updated.version},
            )
            await session.commit()
            return {"autonomy": autonomy, "profile_version": updated.version}
        if name == "reset_measurement":
            metric = str(args.get("metric", ""))
            defn = next((r for r in profile.rhythms if r.get("name") == metric), None)
            if defn is None:
                return {"error": f"no measurement called {metric!r}"}
            try:
                cutoff = datetime.fromisoformat(str(args.get("from_date", "")).replace("Z", "+00:00"))
            except ValueError:
                return {"error": "from_date must be a date like 2026-03-01"}
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=UTC)
            baseline = await reset_norms(session, company_id, metric, cutoff, reset_by="agent", defn=defn)
            await audit.record(
                session, company_id, "agent", "norm.reset", target=metric,
                metadata={"from_date": cutoff.isoformat()},
            )
            await session.commit()
            return {
                "metric": metric,
                "from_date": cutoff.date().isoformat(),
                "now_typically": f"{baseline.median} {baseline.unit}" if baseline else "nothing left to measure",
                "based_on": baseline.n if baseline else 0,
                "effect": f"{metric} now ignores anything before {cutoff.date().isoformat()}",
            }
        if name == "describe_system":
            return await describe(session, profile)
        if name == "run_action":
            action = str(args.get("action", ""))
            if action not in (profile.moves.get("registry") or {}):
                return {"error": f"unknown action {action!r}"}
            result = await request_action(
                session, profile, action=action,
                params={"argument": str(args.get("argument", ""))} if args.get("argument") else {},
                situation_id=args.get("situation_id"),
                requested_by="agent",
                # the agent's typed intent is not a precise per-action approval:
                # anything the profile marks approval_required still queues for a human
                pre_approved=False,
            )
            policy = await approval_policy(session, profile)
            return {
                "action": action, "status": result.status, "detail": result.detail,
                "dry_run": policy["dry_run"],
            }
        return {"error": f"unknown tool {name!r}"}

    return dispatch


# Tools that CHANGE something. A reply that claims a change without one of
# these having run is a fabrication, and the UI says so rather than letting
# the prose stand as the record.
_CHANGING_TOOLS = {
    "run_action": "action",
    "set_practice_mode": "practice mode",
    "set_action_approval": "approval policy",
    "set_autonomy": "autonomy policy",
    "reset_measurement": "a learned measurement",
}


_TOOL_VERB = {
    "search_events": "searched your events",
    "list_situations": "checked what needs attention",
    "get_briefing": "read the summary",
    "get_norms": "looked up what's normal",
    "reset_measurement": "recalculated a measurement",
    "describe_system": "reviewed what it knows",
    "run_action": "ran an action",
    "set_practice_mode": "changed practice mode",
    "set_action_approval": "changed the approval policy",
    "set_autonomy": "changed how independently it works",
}


def _trace_artifact(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The work behind the answer, as a collapsible record.

    Two jobs: it shows a person HOW an answer was reached instead of asking
    them to take prose on faith, and it is the receipt that catches a model
    claiming work it never did — the steps are real or the line is not there.
    """
    if not steps:
        return None
    return {
        "type": "trace",
        "steps": [
            {
                "tool": s["tool"],
                "label": _TOOL_VERB.get(s["tool"], s["tool"].replace("_", " ")),
                "detail": _step_detail(s),
            }
            for s in steps
        ],
    }


def _step_detail(step: dict[str, Any]) -> str:
    """One short line of what this step actually found or did."""
    result = step.get("result") or {}
    if err := result.get("error"):
        return f"failed: {err}"
    tool = step["tool"]
    if tool == "search_events":
        return f"{result.get('count', 0)} match(es) for “{step.get('arguments', {}).get('query', '')}”"
    if tool == "list_situations":
        return f"{len(result.get('situations', []))} open"
    if tool == "get_norms":
        known = [m for m in result.get("measurements", []) if m.get("known")]
        return f"{len(known)} measured, {len(result.get('measurements', [])) - len(known)} not yet"
    if tool == "run_action":
        return f"{result.get('action', '')} — {result.get('status', '')}"
    if tool in ("set_practice_mode", "set_action_approval", "set_autonomy", "reset_measurement"):
        return str(result.get("effect") or "changed")
    return "done"


def _artifacts_from_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn the tool trace into inline chat artifacts the Feed/Agent already
    render — evidence blocks for searches, chips for what actually changed.

    The chips are the receipt. The model's prose is not evidence: it can
    (and does) claim to have changed things it never touched, so anything
    the user is meant to believe has to come from a step that really ran.
    """
    artifacts: list[dict[str, Any]] = []
    for step in steps:
        result = step.get("result", {})
        if step["tool"] == "set_practice_mode" and "practice_mode" in result:
            artifacts.append({
                "type": "chip",
                "label": f"Practice mode {'ON' if result['practice_mode'] else 'OFF'} — {result.get('effect', '')}",
            })
        elif step["tool"] == "set_action_approval" and "approval_required" in result:
            artifacts.append({"type": "chip", "label": result.get("effect", "approval policy changed")})
        elif step["tool"] == "set_autonomy" and "autonomy" in result:
            allowed = ", ".join(result["autonomy"].get("allowed_actions", [])) or "nothing"
            artifacts.append({"type": "chip", "label": f"Now allowed to act alone on: {allowed}"})
        elif step["tool"] == "search_events" and result.get("results"):
            artifacts.append(
                {
                    "type": "evidence",
                    "title": f"EVIDENCE · {result['count']} EVENTS",
                    "items": [
                        {
                            "event_id": r["id"], "source": r["source"],
                            "timestamp": r["timestamp"], "excerpt": r["excerpt"], "url": r.get("url"),
                        }
                        for r in result["results"]
                    ],
                }
            )
        elif step["tool"] == "run_action" and "status" in result:
            verb = {
                "pending_approval": "queued for approval",
                "dry_run": "rehearsed (Practice mode)",
                "executed": "done",
                # say what actually happened: a log move contacted nobody
                "recorded": "noted (no external system contacted)",
            }.get(result["status"], result["status"])
            artifacts.append({"type": "chip", "label": f"{result['action']} — {verb}"})
    return artifacts


async def answer(
    session: AsyncSession,
    profile: Profile,
    user_message: str,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one agent turn against the real connected company. Returns
    ``{"reply", "artifacts", "steps"}`` — the caller persists it."""
    system_prompt = await _system_prompt(session, profile)
    tools = build_tools(profile)
    dispatch = _make_dispatch(session, profile)
    outcome = await run_tool_loop(system_prompt, user_message, tools, dispatch, history=history)
    steps = outcome["steps"]
    artifacts = _artifacts_from_steps(steps)
    if trace := _trace_artifact(steps):
        artifacts.insert(0, trace)  # the work comes before the receipts
    return {
        "reply": outcome["reply"],
        "artifacts": artifacts,
        "steps": steps,
        # The verified record of what this turn really did. The UI shows it
        # under the reply, so a model that writes "I've updated the system"
        # without calling a tool is contradicted on screen instead of
        # believed. Prose is not evidence; this is.
        "tools_used": [s["tool"] for s in steps],
        "changed": sorted({
            _CHANGING_TOOLS[s["tool"]] for s in steps
            if s["tool"] in _CHANGING_TOOLS and not (s.get("result") or {}).get("error")
        }),
    }
