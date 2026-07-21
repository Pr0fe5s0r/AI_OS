from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.common.clarifications import create_norm_drift_clarifications
from apps.common.context import (
    approval_policy,
    routing_config,
    team_roster,
)
from apps.common.feed_stream import publish_feed_update
from packages.core.act import act
from packages.core.agent import choose_assignee, decide_action
from packages.core.brief import assemble_brief
from packages.core.deliver import deliver
from packages.core.detect import run_watcher_engine
from packages.core.norms import NormBaseline, compute_baselines, get_norms
from packages.core.profile import Profile
from packages.core.situations import (
    get_situation,
    record_delivery,
    resolve_stale,
    save_situation,
)
from packages.core.store import get_event
from packages.core.tickets import create_ticket, ticket_for_situation
from packages.core.tokens import issue_token
from packages.core.workload import open_workload
from packages.shared.schema import (
    ActionDecision,
    ActionLink,
    ActionRequest,
    ActionResult,
    Brief,
    Situation,
    Ticket,
)

# The analysis loop, orchestrated for ANY profile: learn rhythms -> run
# watchers -> propose an owner -> deliver the brief -> autonomous follow-up.
# Every rule, prompt, move and threshold arrives from the profile row.

ASSIGN_PURPOSE = "assign"


def base_url() -> str:
    import os

    return os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


# ------------------------- move params (data-driven) -------------------------


def extract_target(profile: Profile, url: str | None) -> dict[str, str] | None:
    """Map an evidence URL onto named action params via the profile's pattern."""
    pattern = profile.moves.get("target_url_pattern")
    names = profile.moves.get("target_params", [])
    if not pattern or not names or not url:
        return None
    match = re.search(pattern, url)
    if not match:
        return None
    return dict(zip(names, match.groups(), strict=False))


def _fill(value: Any, values: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return value.format(**values) if "{" in value else value
    if isinstance(value, dict):
        return {k: _fill(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, values) for v in value]
    return value


def params_for_move(
    profile: Profile, move: str, argument: str, situation: Situation
) -> dict | None:
    """Build the concrete action params a move needs, from its profile spec."""
    spec = (profile.moves.get("registry") or {}).get(move)
    if spec is None:
        return None
    values: dict[str, Any] = {"argument": argument, "situation_id": situation.id}

    template = dict(spec.get("params") or {})
    needs_target = template.pop("target", None)
    if needs_target:
        url = situation.evidence[0].url if situation.evidence else None
        target = extract_target(profile, url)
        if target is None:
            return None
        values.update(target)

    try:
        params = _fill(template, values)
    except KeyError:
        return None
    return {
        "situation_id": situation.id,
        "argument": argument,
        **{k: v for k, v in values.items() if k not in ("argument", "situation_id")},
        **params,
    }


def default_argument(profile: Profile, move: str, situation: Situation) -> str:
    """What a one-click button means for a move, templated from profile data."""
    spec = (profile.moves.get("registry") or {}).get(move, {})
    template = str(spec.get("default_argument", "") or "")
    if not template:
        return situation.recommended_action or situation.summary
    try:
        return template.format(
            title=situation.title,
            severity=situation.severity,
            summary=situation.summary,
            recommended_action=situation.recommended_action or "triage this.",
        )
    except KeyError:
        return template


def action_help(profile: Profile, allowed: list[str]) -> str:
    registry = profile.moves.get("registry") or {}
    lines = []
    for name in allowed:
        hint = str(registry.get(name, {}).get("argument", "") or "no argument needed")
        lines.append(f"  {name} -> argument is {hint}")
    return "\n".join(lines)


def requires_human(situation: Situation, decision: ActionDecision, autonomy: dict) -> bool:
    """Autonomy gate, driven by the profile's autonomy policy."""
    if decision.action == "escalate":
        return True
    if situation.severity in autonomy.get("escalate_severities", []):
        return True
    return decision.confidence < float(autonomy.get("min_confidence", 0.7))


def free_members(profile: Profile, roster: list[dict], workload: dict[str, int]) -> list[dict]:
    """People holding the assignable role with spare capacity right now."""
    team = profile.moves.get("team", {}) or {}
    role = team.get("assignable_role")
    available = []
    for member in roster:
        if not member.get("assignable", True):
            continue
        if role and role not in member.get("roles", []):
            continue
        capacity = int(member.get("max_open_issues", 3))
        open_items = int(workload.get(member["id"], 0))
        if open_items < capacity:
            available.append({**member, "open_issues": open_items, "capacity": capacity})
    return available


# ------------------------------ assignment flow ------------------------------


async def _current_assignee(
    session: AsyncSession, profile: Profile, situation: Situation
) -> str | None:
    if not situation.evidence:
        return None
    event = await get_event(session, profile.company_id, situation.evidence[0].event_id)
    if event is None:
        return None
    team = profile.moves.get("team", {}) or {}
    field = (team.get("workload") or {}).get("assignee_field", "assignee")
    return event.metadata.get(field) or None


async def propose_assignment(
    session: AsyncSession, profile: Profile, situation: Situation, roster: list[dict]
) -> dict:
    """Who is free, who the AI recommends, and one-click links to assign them."""
    team = profile.moves.get("team", {}) or {}
    if not team.get("assign_move"):
        return {"status": "no_assign_move", "assignee": None, "links": []}
    existing = await _current_assignee(session, profile, situation)
    if existing:
        return {"status": "already_owned", "assignee": existing, "links": []}
    if not roster:
        return {"status": "no_team_configured", "assignee": None, "links": []}

    url = situation.evidence[0].url if situation.evidence else None
    target = extract_target(profile, url)
    if target is None:
        return {"status": "no_target", "assignee": None, "links": []}

    workload = await open_workload(session, profile.company_id, team.get("workload", {}))
    candidates = free_members(profile, roster, workload)
    if not candidates:
        return {"status": "nobody_free", "assignee": None, "links": []}

    prompts = profile.vocabulary.get("prompts", {})
    decision = choose_assignee(situation, candidates, prompts.get("choose_assignee", "{title}"))

    item_label = " ".join(str(v) for v in target.values())
    links: list[ActionLink] = []
    for candidate in candidates:
        token = await issue_token(
            session,
            profile.company_id,
            ASSIGN_PURPOSE,
            {
                "situation_id": situation.id,
                "assignee": candidate["id"],
                "target": target,
                "item_label": item_label,
                "event_id": situation.evidence[0].event_id,
                "title": (situation.evidence[0].excerpt or situation.title).splitlines()[0][:120],
                "summary": situation.summary,
            },
        )
        links.append(
            ActionLink(
                label=f"Assign to {candidate.get('name', candidate['id'])}",
                url=f"{base_url()}/api/act/{token}",
                primary=candidate["id"] == decision.assignee,
            )
        )

    who = ", ".join(
        f"{c.get('name', c['id'])} ({c['open_issues']}/{c['capacity']})" for c in candidates
    )
    note = f"Free right now: {who}."
    if decision.assignee != "none":
        note += f" The AI recommends {decision.assignee} — {decision.rationale}"

    return {
        "status": "awaiting_approver",
        "assignee": None,
        "recommended": decision.assignee,
        "confidence": round(decision.confidence, 2),
        "candidates": [c["id"] for c in candidates],
        "links": links,
        "note": note,
    }


async def accept_assignment(
    session: AsyncSession, profile: Profile, payload: dict, clicked_by: str = "email"
) -> dict:
    """Called after a signed, single-use token has been consumed."""
    assignee = payload["assignee"]
    target: dict = payload.get("target", {})
    team = profile.moves.get("team", {}) or {}
    assign_move = team["assign_move"]
    spec = (profile.moves.get("registry") or {})[assign_move]
    policy = await approval_policy(session, profile)

    values = {"argument": assignee, "situation_id": payload["situation_id"], **target}
    template = dict(spec.get("params") or {})
    template.pop("target", None)
    params = {
        **values,
        **_fill(template, values),
        "rationale": f"assigned by {clicked_by}",
    }

    request = ActionRequest(
        company_id=profile.company_id,
        action=assign_move,
        params=params,
        situation_id=payload["situation_id"],
        requested_by=clicked_by,
    )
    result: ActionResult = await act(
        {"session": session}, request, profile.moves.get("registry", {}),
        {**policy, "force_approval": False},
    )

    existing = await ticket_for_situation(session, profile.company_id, payload["situation_id"])
    if existing:
        ticket_id = existing.id
    else:
        url = None
        if situation := await get_situation(session, profile.company_id, payload["situation_id"]):
            url = situation.evidence[0].url if situation.evidence else None
        ticket_id = await create_ticket(
            session,
            Ticket(
                company_id=profile.company_id,
                situation_id=payload["situation_id"],
                title=payload.get("title", f"Fix {payload.get('item_label', '')}"),
                description=payload.get("summary", ""),
                assignee=assignee,
                source_event_id=payload.get("event_id"),
                external_url=url,
            ),
        )
    await session.commit()
    return {
        "assignee": assignee,
        "assign_status": result.status,
        "ticket_id": ticket_id,
        "detail": result.detail,
        "dry_run": policy["dry_run"],
    }


async def complete_ticket(
    session: AsyncSession, profile: Profile, ticket: Ticket, by: str
) -> dict:
    """The assignee finished: run the profile's close move on the source item."""
    team = profile.moves.get("team", {}) or {}
    close_move = team.get("close_move")
    target = extract_target(profile, ticket.external_url)
    if not close_move or target is None:
        return {"source": "no_close_move" if not close_move else "no_target"}

    spec = (profile.moves.get("registry") or {}).get(close_move, {})
    policy = await approval_policy(session, profile)
    values = {"argument": "", "situation_id": ticket.situation_id or "", **target}
    template = dict(spec.get("params") or {})
    template.pop("target", None)
    params = {
        **values,
        **_fill(template, values),
        "rationale": f"ticket #{ticket.id} completed by {by}",
    }

    request = ActionRequest(
        company_id=profile.company_id,
        action=close_move,
        params=params,
        situation_id=ticket.situation_id,
        requested_by=by,
    )
    result = await act(
        {"session": session}, request, profile.moves.get("registry", {}),
        {**policy, "force_approval": False},
    )
    await session.commit()
    return {"source": result.status, "detail": result.detail, "dry_run": policy["dry_run"]}


# ------------------------------ autonomous step ------------------------------


async def _situation_already_actioned(
    session: AsyncSession, company_id: str, situation_id: str, dry_run: bool
) -> bool:
    """Has a prior autonomous pass already left this situation with an action
    that makes re-acting pointless? If so the watcher engine, which re-runs
    every 5 minutes, must NOT decide again: it would burn an LLM call and
    (before act()'s idempotency guard) stack up duplicate rows.

    Always terminal: ``pending_approval`` (waiting on a human), ``executed``
    (already done for real) and ``recorded`` (a `log` move already captured
    the intent — re-logging it every 5 minutes adds nothing). ``dry_run`` is
    terminal ONLY while Practice mode is still on — re-rehearsing the same
    move every pass adds nothing; but the moment the operator turns Practice
    mode off, a situation that has only ever been rehearsed should get a real
    attempt, so we let it through then.
    """
    skip = ["pending_approval", "executed", "recorded"]
    if dry_run:
        skip.append("dry_run")
    row = (
        await session.execute(
            text(
                """
                SELECT 1 FROM actions
                WHERE company_id = :c AND situation_id = :sid
                  AND status = ANY(:skip)
                LIMIT 1
                """
            ),
            {"c": company_id, "sid": situation_id, "skip": skip},
        )
    ).first()
    return row is not None


async def _autonomous_step(
    session: AsyncSession, profile: Profile, situation: Situation, policy: dict
) -> dict:
    if await _situation_already_actioned(
        session, profile.company_id, situation.id, bool(policy.get("dry_run", True))
    ):
        return {
            "situation_id": situation.id,
            "status": "already_actioned",
            "autonomous": False,
            "skipped": True,
        }

    autonomy = profile.moves.get("autonomy", {}) or {}
    team = profile.moves.get("team", {}) or {}
    prompts = profile.vocabulary.get("prompts", {})
    assign_move = team.get("assign_move")
    allowed = [a for a in autonomy.get("allowed_actions", []) if a != assign_move]

    decision = decide_action(
        situation, allowed, prompts.get("choose_action", "{title} {actions}"),
        action_help=action_help(profile, allowed),
    )

    action_name = decision.action
    params = (
        None if action_name == "escalate"
        else params_for_move(profile, action_name, decision.argument, situation)
    )

    # Two very different reasons we might not act, and they must not be
    # conflated. If the MODEL chose to escalate, a human genuinely needs to
    # decide — raise it. But if we simply could not build the parameters (a
    # graph-raised situation has no URL, so there is no repo/number to act
    # on), then nothing is actionable here and asking a human to "approve"
    # our inability is pure noise. The situation is already on the feed; leave
    # it there and stay quiet.
    if decision.action != "escalate" and params is None:
        return {
            "situation_id": situation.id,
            "chose": decision.action,
            "status": "not_actionable",
            "autonomous": False,
            "skipped": True,
        }

    escalated = decision.action == "escalate"
    if escalated:
        action_name = autonomy.get("escalation_action", "escalate")
        params = {
            "situation_id": situation.id,
            "argument": decision.rationale or "needs human judgement",
        }

    human = escalated or requires_human(situation, decision, autonomy)
    final_params: dict = {
        **(params or {}),
        "rationale": decision.rationale,
        "confidence": round(decision.confidence, 2),
    }

    request = ActionRequest(
        company_id=profile.company_id,
        action=action_name,
        params=final_params,
        situation_id=situation.id,
        requested_by="ai",
    )
    result: ActionResult = await act(
        {"session": session}, request, profile.moves.get("registry", {}),
        {**policy, "force_approval": human},
    )
    return {
        "situation_id": situation.id,
        "chose": decision.action,
        "ran": action_name,
        "argument": decision.argument,
        "confidence": round(decision.confidence, 2),
        "status": result.status,
        "autonomous": result.status != "pending_approval",
    }


# --------------------------------- the loop ---------------------------------


async def evaluate_watchers(
    session: AsyncSession, profile: Profile, norms: list[NormBaseline]
) -> dict:
    """Detect (universal built-ins + profile watchers, checkpoint 2 part C)
    -> save -> propose owner -> deliver -> retire stale -> autonomous
    follow-up. The ONE place business situations get raised, called from
    BOTH the business scan's cadence (run_analysis, below) and the watcher
    engine's own faster 5-min cron (apps.common.watching) — so whichever
    triggers it, resolve_stale's "what's still true" set is always a
    complete watcher-engine pass, never a partial one racing another.
    """
    company_id = profile.company_id
    prompts = profile.vocabulary.get("prompts", {})
    state = {"session": session, "company_id": company_id, "prompts": prompts}
    situations = await run_watcher_engine(state, profile, norms)

    policy = await approval_policy(session, profile)
    roster = await team_roster(session, company_id)
    routing = routing_config(profile, policy["dry_run"], roster)

    delivered = 0
    assignments: list[dict] = []
    for situation in situations:
        await save_situation(session, situation)

        proposal = await propose_assignment(session, profile, situation, roster)
        assignments.append(
            {"situation_id": situation.id, **{k: v for k, v in proposal.items() if k != "links"}}
        )

        brief: Brief = assemble_brief(situation)
        brief.assigned_to = proposal.get("assignee")
        brief.action_links = proposal.get("links", [])
        brief.note = proposal.get("note", "")
        receipt = deliver(brief, routing)
        await record_delivery(session, receipt)
        delivered += 1

    resolved = await resolve_stale(session, company_id, [s.id for s in situations], kind="business")
    await session.commit()
    await publish_feed_update(company_id, len(situations))

    autonomy = profile.moves.get("autonomy", {}) or {}
    decisions: list[dict] = []
    if autonomy.get("enabled"):
        for situation in situations[: int(autonomy.get("max_actions_per_run", 5))]:
            decisions.append(await _autonomous_step(session, profile, situation, policy))
        await session.commit()

    by_severity: dict[str, int] = {}
    for s in situations:
        by_severity[s.severity] = by_severity.get(s.severity, 0) + 1

    return {
        "situations": len(situations),
        # what this pass had on the feed, in the shape event-triggered workflows
        # match on. Returned rather than acted on here: the trigger runtime
        # imports this module, so firing from inside it would be a cycle — and
        # the caller (a cron entrypoint) is the honest owner of side effects.
        "raised": [{"id": s.id, "rule": s.rule, "severity": s.severity} for s in situations],
        "delivered": delivered,
        "resolved": resolved,
        "by_severity": by_severity,
        "dry_run": policy["dry_run"],
        "notified": routing["routes"]["high"]["recipients"],
        "assignments": assignments,
        "acted_autonomously": sum(1 for d in decisions if d["autonomous"]),
        "escalated_to_human": sum(1 for d in decisions if not d["autonomous"] and not d.get("skipped")),
        "skipped_already_actioned": sum(1 for d in decisions if d.get("skipped")),
        "decisions": decisions,
    }


async def run_analysis(session: AsyncSession, profile: Profile) -> dict:
    """Learn rhythms -> evaluate watchers -> propose owner -> deliver -> follow-up."""
    company_id = profile.company_id
    await compute_baselines(session, company_id, profile.rhythms)
    await session.commit()
    norms = await get_norms(session, company_id)
    clarifications = await create_norm_drift_clarifications(session, profile)

    watcher_summary = await evaluate_watchers(session, profile, norms)

    return {
        "norms": len(norms),
        "clarifications": len(clarifications),
        **watcher_summary,
    }


async def request_action(
    session: AsyncSession,
    profile: Profile,
    action: str,
    params: dict,
    situation_id: str | None = None,
    requested_by: str = "system",
    pre_approved: bool = True,
    force_approval: bool = False,
) -> ActionResult:
    """Ask the action runtime to run a registered move.

    The UI only knows the situation; the move's param template (profile data)
    derives the rest, so a button can never queue an action guaranteed to fail.

    ``pre_approved`` defaults to True for the UI path: a human clicking a
    specific button IS the approval, so it runs in one step (dry_run still
    applies in Practice mode). The agent (checkpoint 4) passes False — a typed
    request like "escalate this" is a high-level intent, not a precise
    approval of the exact move, so any action the profile marks
    approval_required must still queue for a human. Either way, dry_run
    protects external writes while Practice mode is on.

    ``force_approval`` makes an otherwise auto-runnable action queue anyway —
    how a workflow run enforces "auto-run allowlisted actions only": a step
    whose action is NOT on the autonomy allowlist is forced to the approval
    queue instead of firing unattended.
    """
    if situation_id:
        situation = await get_situation(session, profile.company_id, situation_id)
        if situation is not None:
            argument = str(params.get("argument") or default_argument(profile, action, situation))
            derived = params_for_move(profile, action, argument, situation)
            if derived is not None:
                params = {**derived, **params}  # explicit params always win

    request = ActionRequest(
        company_id=profile.company_id,
        action=action,
        params=params,
        situation_id=situation_id,
        requested_by=requested_by,
    )
    policy = await approval_policy(session, profile)
    result = await act(
        {"session": session}, request, profile.moves.get("registry", {}),
        {**policy, "pre_approved": pre_approved, "force_approval": force_approval or policy.get("force_approval", False)},
    )
    await session.commit()
    return result




