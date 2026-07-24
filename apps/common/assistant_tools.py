from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apps.common.analysis import request_action
from apps.common.context import DRY_RUN_KEY, approval_policy
from apps.common.understanding import describe
from packages.core import audit
from packages.core.assistant import run_tool_loop, run_tool_loop_streaming
from packages.core.briefing import build_briefing
from packages.core.items import item_facets, list_items
from packages.core.norms import get_norms, reset_norms
from packages.core.orientation import BASELINE_TERMS, onboarding_state, orientation_prompt
from packages.core.profile import Profile, set_action_approval, set_autonomy
from packages.core.search import search
from packages.core.settings import set_setting
from packages.core.situations import list_situations
from packages.core.store import get_event, list_transitions

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


async def _resolve_record_id(
    session: AsyncSession, company_id: str, given: str
) -> str | None:
    """Turn what a person called a record into the id we actually store.

    Records are keyed by the source's own id — GitHub's are bare numbers, "10".
    People say "PR 10", and the model passes that straight through, so the
    lookup missed and the tool answered as if the record did not exist. That
    produced a confidently wrong answer about a pull request with two commits,
    and nothing in the trace said why: it looked like an empty history rather
    than a failed lookup.

    So try the exact string first, then each word in it, longest first. Generic
    by construction — no source's naming is assumed, only that the id a person
    quotes usually appears somewhere in what they typed.
    """
    given = given.strip()
    if not given:
        return None
    if await get_event(session, company_id, given) is not None:
        return given
    tokens = sorted(set(re.findall(r"[A-Za-z0-9_.-]+", given)), key=len, reverse=True)
    for token in tokens:
        if token != given and await get_event(session, company_id, token) is not None:
            return token
    # Nothing matched. Return what was asked for rather than None: the history
    # query will come back empty, which is the truthful answer for a record we
    # do not hold, and the trace will name what was looked for.
    return given


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
                "name": "list_work",
                "description": (
                    "The full inventory of work records with their lifecycle status "
                    "(e.g. open/closed) and a breakdown by status, source and type. "
                    "Use this — NOT search_events — for any question about how much "
                    "work exists, how much is open or closed, or what is oldest. "
                    "search_events finds records by meaning and cannot count."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "only records in this status"},
                        "source": {"type": "string", "description": "only records from this tool"},
                        "limit": {"type": "integer", "description": "max records listed (default 20)"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_changes",
                "description": (
                    "What has CHANGED about the work — a record moving from open to "
                    "closed or merged, being retitled, or gaining commits and discussion "
                    "— with what it changed from, to, and when we saw it. Use it for "
                    "'what happened', 'what moved', or any question about a record's past. "
                    "Pass record_id to ask about one record: the reply then ALSO carries "
                    "that record — including `reports`, the record's own account of its "
                    "commits, changed files and discussion — so one call answers the "
                    "whole question. An empty `changes` list only means nothing changed "
                    "since we started watching, never that nothing happened."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "record_id": {"type": "string", "description": "limit to one record's history"},
                        "limit": {"type": "integer", "description": "max changes (default 25)"},
                    },
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
                    "escalate_severities = severities that ALWAYS reach a human. "
                    "allow_public_actions = let it post where teammates or customers can read "
                    "it (comments, reviews, new issues) without asking first; off by default, "
                    "and allowlisting such a move does NOT enable it on its own."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "allowed_actions": {"type": "array", "items": {"type": "string", "enum": action_names}},
                        "min_confidence": {"type": "number"},
                        "escalate_severities": {"type": "array", "items": {"type": "string"}},
                        "enabled": {"type": "boolean"},
                        "allow_public_actions": {"type": "boolean"},
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
    thing_word = vocab.get("thing", BASELINE_TERMS["thing"])
    # what's true of any organization, plus where THIS one actually is. Without
    # a seed profile a new company starts blank, and an agent that knows nothing
    # on day one is useless exactly when someone is forming an opinion of it.
    orientation = orientation_prompt(await onboarding_state(session, profile.company_id, profile))
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
        f"{orientation}\n\n"
        f"You are the operations agent for a company (its work is called \"{thing_word}s\"). "
        "You run this system. The user should be able to do ANYTHING by talking to you — "
        "ask about the data, take action on it, or change how the system itself behaves.\n\n"
        "Use the tools to ground every answer in the company's REAL data — never invent "
        "events, numbers, or situation ids. If a search returns nothing, say so plainly.\n"
        "COUNTING: any question about how much work exists, how much is open or closed, "
        "or what is oldest, goes to list_work. search_events ranks by meaning and returns "
        "a handful of matches — counting its results reports a fraction as the whole.\n\n"
        "EARLIER TURNS ARE A TRANSCRIPT, NOT THE CURRENT STATE. Each of your earlier "
        "replies is stamped with when you said it. Records arrive continuously and this "
        "system keeps learning, so any count, name, date or \"nothing has arrived yet\" "
        "in an earlier turn describes a moment that has passed. NEVER carry a fact "
        "forward from history — re-read it with a tool, every time. When a tool "
        "contradicts something you said before, the tool is right: say plainly that it "
        "has changed rather than defending the old answer.\n\n"
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

    # which metadata field holds a record's lifecycle state is PROFILE data
    # ("state" for GitHub, "status" elsewhere) — the same lookup the Work
    # screen uses, so the agent and the screen can never disagree
    status_field = (profile.things or {}).get("status_field")
    activity_field = (profile.things or {}).get("activity_field")

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
                        # without this the model saw nine records and no way to
                        # tell a finished one from a live one, so it reported
                        # every closed issue as open
                        "status": (r.get("metadata") or {}).get(status_field) if status_field else None,
                    }
                    for r in results
                ],
            }
        if name == "list_work":
            now = datetime.now(UTC)
            limit = min(int(args.get("limit") or 20), 50)
            status = args.get("status") or None
            source = args.get("source") or None
            facets = await item_facets(
                session, company_id, status_field=status_field, source=source, status=status
            )
            records = await list_items(
                session, company_id, status_field=status_field,
                source=source, status=status, limit=limit,
            )
            # item_facets returns PLURAL keys (sources/types/statuses). Reading
            # the singular here handed the model total=0 and every breakdown
            # empty, so it had no counts at all and fell back to eyeballing the
            # capped records list — which is why it answered "9 issues" one time
            # and "4 pull requests" the next. The counts were always available;
            # they just never reached it.
            return {
                "total": sum(f["count"] for f in facets.get("sources", [])),
                "by_status": facets.get("statuses", []),
                "by_source": facets.get("sources", []),
                "by_type": facets.get("types", []),
                # age is COMPUTED here, not left as a timestamp for the model to
                # subtract: asked how long the oldest issue had been open, it
                # read 2026-07-10 and answered "1 day, 9 hours" for something
                # twelve days old. Arithmetic is the tool's job.
                "records": [
                    {
                        "id": r["id"], "title": r["title"], "status": r["status"],
                        # what the record says past its title — a description,
                        # which files a pull request touched, the commit
                        # messages on it. Without this the model knows a name
                        # and a status and will fill the rest in itself.
                        "detail": r["detail"],
                        "source": r["source"], "type": r["type"],
                        "opened": r["timestamp"], "owner": r["actor"], "url": r["url"],
                        "age_hours": round(
                            (now - datetime.fromisoformat(r["timestamp"])).total_seconds() / 3600, 1
                        ),
                    }
                    for r in records
                ],
            }
        if name == "list_changes":
            record_id = await _resolve_record_id(
                session, company_id, str(args.get("record_id") or "")
            )
            changes = await list_transitions(
                session, company_id, record_id=record_id,
                limit=min(int(args.get("limit") or 25), 100),
            )
            answer: dict[str, Any] = {"count": len(changes), "changes": changes}

            # Asked about ONE record, answer with the record too.
            #
            # The transition log starts the moment we first see a record
            # differ, so everything that happened before we were watching lives
            # in what the record SAYS, not in its history. Told only that the
            # history was empty, the model reported a pull request carrying two
            # commits as "completely untouched since creation". Telling it to
            # go and call list_work next did not work either — it answered from
            # the one result it had.
            #
            # So the fix is not a better instruction, it is not splitting the
            # answer in the first place: a question about a record returns that
            # record's current state beside its history, and the empty case
            # stops being the whole story.
            if record_id:
                event = await get_event(session, company_id, record_id)
                if event is not None:
                    lines = (event.content or "").strip().splitlines()
                    answer["record"] = {
                        "id": event.id,
                        "title": lines[0][:200] if lines else event.id,
                        # The record's own account of itself: commits, files
                        # changed, discussion. Named `reports` rather than
                        # `detail` because a field's NAME is a cue for what it
                        # is worth — "detail" reads as optional trimming, and
                        # got left out of the answer.
                        "reports": "\n".join(lines[1:]).strip()[:800],
                        "status": event.metadata.get(status_field) if status_field else None,
                        "url": (event.metadata or {}).get("url"),
                        "last_changed": (event.metadata or {}).get(activity_field)
                        if activity_field else None,
                    }
            if not changes:
                # Plain fact, not an instruction. Telling the model to "read
                # record.detail before concluding X" put those words in the
                # tool result, and it relayed them to the USER — "Check
                # `record.detail` for commits" as if that were the finding.
                # A tool result is data; anything imperative in it gets
                # summarised and shown to a person. So state what is true and
                # let the record's own words be the material for the answer.
                answer["history"] = (
                    "Nothing about this record has changed since MarkOS began watching "
                    "it. Anything that happened before then is described by the record "
                    "itself, not by this history."
                )
            return answer
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
    if tool == "list_work":
        return f"{result.get('total', 0)} record(s)"
    if tool == "list_changes":
        # Name the record it asked about. "done" told us nothing when this tool
        # was answering wrongly — we could not tell from the trace whether the
        # model had scoped the question to a record at all, which is exactly
        # what the trace exists to show.
        asked = step.get("arguments", {}).get("record_id")
        scope = f"record {asked}" if asked else "all records"
        return f"{result.get('count', 0)} change(s), {scope}"
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


def _ago(seconds: float) -> str:
    if seconds < 120:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} hours ago"
    return f"{round(seconds / 86400)} days ago"


def history_for_model(prior: list[Any], now: datetime | None = None) -> list[dict[str, Any]]:
    """Prior turns as the model should see them: its own replies STAMPED with
    when it said them.

    Replaying an assistant turn unstamped hands the model its own past
    assertions as if they were present tense, and a model trusts its own prior
    words over a tool result. This workspace watched it happen: while its
    events were misfiled, the agent truthfully said "no records have arrived
    yet" — then kept repeating it from history after nine records had arrived
    and every screen showed them.

    Only assistant turns are stamped. A user's question doesn't decay; an
    answer built from data does.
    """
    now = now or datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for m in prior:
        role = getattr(m, "role", None)
        content = getattr(m, "content", None)
        if not content or role not in ("user", "agent"):
            continue
        if role == "user":
            out.append({"role": "user", "content": content})
            continue
        created = getattr(m, "created_at", None)
        stamp = _ago((now - created).total_seconds()) if created else "earlier"
        out.append({
            "role": "assistant",
            "content": f"[said {stamp}, describing the data as it stood then]\n{content}",
        })
    return out


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


def _outcome(reply: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble the same turn record `answer` returns, from a finished loop."""
    artifacts = _artifacts_from_steps(steps)
    if trace := _trace_artifact(steps):
        artifacts.insert(0, trace)
    return {
        "reply": reply,
        "artifacts": artifacts,
        "steps": steps,
        "tools_used": [s["tool"] for s in steps],
        "changed": sorted({
            _CHANGING_TOOLS[s["tool"]] for s in steps
            if s["tool"] in _CHANGING_TOOLS and not (s.get("result") or {}).get("error")
        }),
    }


async def answer_streaming(
    session: AsyncSession,
    profile: Profile,
    user_message: str,
    history: list[dict[str, Any]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """`answer` as a live stream. Forwards the loop's events untouched and ends
    with one ``{"type": "final", ...}`` carrying exactly the payload `answer`
    would have returned — so the persisted turn is identical whether the caller
    streamed it or not, and a refresh shows the same thing either way.

    Tool events reuse the trace's own verbs (`_TOOL_VERB`) so what a person
    watches is "reading what needs attention", not `list_situations`."""
    system_prompt = await _system_prompt(session, profile)
    tools = build_tools(profile)
    dispatch = _make_dispatch(session, profile)

    async for event in run_tool_loop_streaming(
        system_prompt, user_message, tools, dispatch, history=history
    ):
        if event["type"] == "final":
            yield {"type": "final", **_outcome(event["reply"], event["steps"])}
        elif event["type"] in ("tool_start", "tool_done"):
            yield {**event, "label": _TOOL_VERB.get(event["tool"], event["tool"].replace("_", " "))}
        else:
            yield event
