from __future__ import annotations

import os
import re

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.act import act
from packages.core.agent import choose_assignee, decide_action
from packages.core.brief import assemble_brief
from packages.core.deliver import deliver
from packages.core.detect import detect
from packages.core.norms import get_norms
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
from verticals.software.config import (
    ACTION_REGISTRY,
    ASSIGNABLE_ROLE,
    AUTONOMY_POLICY,
    COMPANY_ID,
    DETECTION_RULES,
    LLM_PROMPTS,
    WORKLOAD_SPEC,
    approval_policy,
    routing_config,
    team_roster,
)
from verticals.software.understand import compute_norms

# The vertical composes the core. It supplies the rules/prompts/registry and the
# autonomy policy; the core does detection, briefing, routing and execution.
#
# Assignment workflow (project manager in the loop):
#   issue with no owner -> AI analyses -> email PM/TL listing who is FREE, with a
#   one-click button per candidate -> PM clicks -> AI assigns on GitHub and opens
#   a ticket for that person -> they finish it and close the ticket -> the GitHub
#   issue is closed.

_GITHUB_URL = re.compile(r"github\.com/([^/]+/[^/]+)/(?:issues|pull)/(\d+)")

ASSIGN_PURPOSE = "assign"


def base_url() -> str:
    """Where the one-click links point. Must be reachable from the PM's inbox."""
    return os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


def repo_and_number(url: str | None) -> tuple[str, str] | None:
    match = _GITHUB_URL.search(url or "")
    return (match.group(1), match.group(2)) if match else None


def requires_human(situation: Situation, decision: ActionDecision, policy: dict) -> bool:
    """Autonomy gate owned by the vertical. The core enforces it via force_approval."""
    if decision.action == "escalate":
        return True
    if situation.severity in policy.get("escalate_severities", []):
        return True
    return decision.confidence < float(policy.get("min_confidence", 0.7))


def free_engineers(roster: list[dict], workload: dict[str, int]) -> list[dict]:
    """Only people who are assignable, hold the engineer role, and have spare capacity."""
    available = []
    for member in roster:
        if not member.get("assignable", True):
            continue
        if ASSIGNABLE_ROLE not in member.get("roles", []):
            continue
        capacity = int(member.get("max_open_issues", 3))
        open_issues = int(workload.get(member["id"], 0))
        if open_issues < capacity:
            available.append({**member, "open_issues": open_issues, "capacity": capacity})
    return available


def _params_for(action: str, argument: str, situation: Situation) -> dict | None:
    """Map the agent's opaque `argument` into concrete action params."""
    base = {"situation_id": situation.id}
    if action in ("draft_reply", "page_engineer"):
        return {**base, "argument": argument}

    url = situation.evidence[0].url if situation.evidence else None
    found = repo_and_number(url)
    if not found:
        return None
    repo, number = found
    target = {**base, "repo": repo, "number": number}

    if action == "apply_label":
        return {**target, "body": {"labels": [argument or "triage"]}}
    if action == "assign_issue":
        return {**target, "body": {"assignees": [argument]}}
    if action == "comment_on_pr":
        return {**target, "body": {"body": argument}}
    if action == "close_issue":
        return {**target, "body": {"state": "closed"}}
    return None


# --------------------------------------------------------------------------
# Steps 1-2: analyse an unowned issue, then email the PM/TL with one button per
# free engineer. Nothing is assigned yet — a human picks.
# --------------------------------------------------------------------------


async def _current_assignee(
    session: AsyncSession, situation: Situation, company_id: str
) -> str | None:
    if not situation.evidence:
        return None
    event = await get_event(session, company_id, situation.evidence[0].event_id)
    return (event.metadata.get("assignee") if event else None) or None


async def propose_assignment(
    session: AsyncSession, situation: Situation, company_id: str, roster: list[dict]
) -> dict:
    """Work out who is free, ask the AI who it recommends, and mint one-click links.

    Used by both the email brief and the Flags screen's Assign button — the PM
    sees the same candidates and the same recommendation in either place.
    """
    existing = await _current_assignee(session, situation, company_id)
    if existing:
        return {"status": "already_owned", "assignee": existing, "links": []}
    if not roster:
        return {"status": "no_team_configured", "assignee": None, "links": []}

    target = repo_and_number(situation.evidence[0].url if situation.evidence else None)
    if not target:
        return {"status": "no_target", "assignee": None, "links": []}
    repo, number = target

    workload = await open_workload(session, company_id, WORKLOAD_SPEC)
    candidates = free_engineers(roster, workload)
    if not candidates:
        return {"status": "nobody_free", "assignee": None, "links": []}

    decision = choose_assignee(situation, candidates, LLM_PROMPTS["choose_assignee"])

    links: list[ActionLink] = []
    for candidate in candidates:
        token = await issue_token(
            session,
            company_id,
            ASSIGN_PURPOSE,
            {
                "situation_id": situation.id,
                "assignee": candidate["id"],
                "repo": repo,
                "number": number,
                "event_id": situation.evidence[0].event_id,
                # the ticket should be titled after the issue, not the rule
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
        "status": "awaiting_project_manager",
        "assignee": None,
        "recommended": decision.assignee,
        "confidence": round(decision.confidence, 2),
        "candidates": [c["id"] for c in candidates],
        "links": links,
        "note": note,
    }


# --------------------------------------------------------------------------
# Steps 3-4: the PM clicked a link. Assign on GitHub, then open a ticket.
# --------------------------------------------------------------------------


async def accept_assignment(
    session: AsyncSession, payload: dict, company_id: str, clicked_by: str = "email"
) -> dict:
    """Called after a signed, single-use email token has been consumed."""
    assignee, repo, number = payload["assignee"], payload["repo"], payload["number"]
    policy = await approval_policy(session, company_id)

    request = ActionRequest(
        company_id=company_id,
        action="assign_issue",
        params={
            "situation_id": payload["situation_id"],
            "repo": repo,
            "number": number,
            "body": {"assignees": [assignee]},
            "rationale": f"assigned by {clicked_by}",
        },
        situation_id=payload["situation_id"],
        requested_by=clicked_by,
    )
    result: ActionResult = await act(
        {"session": session}, request, ACTION_REGISTRY, {**policy, "force_approval": False}
    )

    existing = await ticket_for_situation(session, company_id, payload["situation_id"])
    if existing:
        ticket_id = existing.id
    else:
        ticket_id = await create_ticket(
            session,
            Ticket(
                company_id=company_id,
                situation_id=payload["situation_id"],
                title=payload.get("title", f"Fix {repo}#{number}"),
                description=payload.get("summary", ""),
                assignee=assignee,
                source_event_id=payload.get("event_id"),
                external_url=f"https://github.com/{repo}/issues/{number}",
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


# --------------------------------------------------------------------------
# Steps 5-6: the ticket is finished -> close it -> close the GitHub issue.
# --------------------------------------------------------------------------


async def complete_ticket(
    session: AsyncSession, ticket: Ticket, company_id: str, by: str
) -> dict:
    target = repo_and_number(ticket.external_url)
    if not target:
        return {"github": "no_target"}
    repo, number = target
    policy = await approval_policy(session, company_id)

    request = ActionRequest(
        company_id=company_id,
        action="close_issue",
        params={
            "repo": repo,
            "number": number,
            "body": {"state": "closed"},
            "situation_id": ticket.situation_id,
            "rationale": f"ticket #{ticket.id} completed by {by}",
        },
        situation_id=ticket.situation_id,
        requested_by=by,
    )
    result = await act(
        {"session": session}, request, ACTION_REGISTRY, {**policy, "force_approval": False}
    )
    await session.commit()
    return {"github": result.status, "detail": result.detail, "dry_run": policy["dry_run"]}


# --------------------------------------------------------------------------
# The autonomous follow-up action (label / reply). Assignment is NOT here — a
# human decides that, by clicking the email.
# --------------------------------------------------------------------------


async def _autonomous_step(
    session: AsyncSession, situation: Situation, company_id: str, policy: dict
) -> dict:
    allowed = [a for a in AUTONOMY_POLICY["allowed_actions"] if a != "assign_issue"]
    decision = decide_action(situation, allowed, LLM_PROMPTS["choose_action"])

    action_name = decision.action
    params = (
        None if action_name == "escalate" else _params_for(action_name, decision.argument, situation)
    )

    escalated = decision.action == "escalate" or params is None
    if escalated:
        action_name = AUTONOMY_POLICY["escalation_action"]
        params = {
            "situation_id": situation.id,
            "argument": decision.rationale or "needs human judgement",
        }

    human = escalated or requires_human(situation, decision, AUTONOMY_POLICY)
    final_params: dict = {
        **(params or {}),
        "rationale": decision.rationale,
        "confidence": round(decision.confidence, 2),
    }

    request = ActionRequest(
        company_id=company_id,
        action=action_name,
        params=final_params,
        situation_id=situation.id,
        requested_by="ai",
    )
    result: ActionResult = await act(
        {"session": session}, request, ACTION_REGISTRY, {**policy, "force_approval": human}
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


async def run_analysis(session: AsyncSession, company_id: str = COMPANY_ID) -> dict:
    """Learn norms -> detect -> propose an owner -> email the PM -> follow-up action."""
    await compute_norms(session, company_id)
    norms = await get_norms(session, company_id)

    state = {"session": session, "company_id": company_id, "prompts": LLM_PROMPTS}
    situations = await detect(state, DETECTION_RULES, norms)

    policy = await approval_policy(session, company_id)
    roster = await team_roster(session, company_id)
    routing = routing_config(policy["dry_run"], roster)

    delivered = 0
    assignments: list[dict] = []
    for situation in situations:
        await save_situation(session, situation)

        proposal = await propose_assignment(session, situation, company_id, roster)
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

    resolved = await resolve_stale(session, company_id, [s.id for s in situations])
    await session.commit()

    decisions: list[dict] = []
    if AUTONOMY_POLICY.get("enabled"):
        for situation in situations[: int(AUTONOMY_POLICY.get("max_actions_per_run", 5))]:
            decisions.append(await _autonomous_step(session, situation, company_id, policy))
        await session.commit()

    by_severity: dict[str, int] = {}
    for s in situations:
        by_severity[s.severity] = by_severity.get(s.severity, 0) + 1

    return {
        "norms": len(norms),
        "situations": len(situations),
        "delivered": delivered,
        "resolved": resolved,
        "by_severity": by_severity,
        "dry_run": policy["dry_run"],
        "notified": routing["routes"]["high"]["recipients"],
        "assignments": assignments,
        "acted_autonomously": sum(1 for d in decisions if d["autonomous"]),
        "escalated_to_human": sum(1 for d in decisions if not d["autonomous"]),
        "decisions": decisions,
    }


def analysis_comment(situation: Situation) -> str:
    """The AI's brief, formatted to live ON the existing GitHub issue.

    Deliberately not a new issue: the developer fixing this is already reading
    this thread, and a duplicate issue would need its own triage and closing.
    """
    return (
        f"**AI analysis** ({situation.severity})\n\n"
        f"{situation.summary}\n\n"
        f"**Suggested next step:** {situation.recommended_action or 'triage this issue.'}"
    )


def _default_argument(action: str, situation: Situation) -> str:
    """What the human's one-click button means for each action."""
    if action == "comment_on_pr":
        return analysis_comment(situation)
    if action == "apply_label":
        return "triage"
    if action in ("draft_reply", "page_engineer"):
        return situation.recommended_action or situation.summary
    return ""


async def request_action(
    session: AsyncSession,
    action: str,
    params: dict,
    situation_id: str | None = None,
    company_id: str = COMPANY_ID,
    requested_by: str = "system",
) -> ActionResult:
    """A human asking the core action runtime to run a registered action.

    The UI only knows the situation, not that `comment_on_pr` needs a repo and an
    issue number. Derive those here so a button can never queue an action that is
    guaranteed to fail.
    """
    if situation_id:
        situation = await get_situation(session, company_id, situation_id)
        if situation is not None:
            argument = str(params.get("argument") or _default_argument(action, situation))
            derived = _params_for(action, argument, situation)
            if derived is not None:
                params = {**derived, **params}  # explicit params always win

    request = ActionRequest(
        company_id=company_id,
        action=action,
        params=params,
        situation_id=situation_id,
        requested_by=requested_by,
    )
    policy = await approval_policy(session, company_id)
    # the human clicking the button IS the approval — run in one step. dry_run
    # still applies: in Practice mode this records instead of sending.
    result = await act(
        {"session": session}, request, ACTION_REGISTRY, {**policy, "pre_approved": True}
    )
    await session.commit()
    return result
