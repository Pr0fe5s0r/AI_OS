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
from packages.core import situations
from packages.core.act import act
from packages.core.agent import choose_assignee, decide_action
from packages.core.brief import assemble_brief
from packages.core.credentials import get_credential
from packages.core.deliver import deliver
from packages.core.detect import run_watcher_engine
from packages.core.norms import NormBaseline, compute_baselines, get_norms
from packages.core.observed import observed_values
from packages.core.profile import Profile
from packages.core.review import review_thing, run_reviewers
from packages.core.situations import (
    get_situation,
    record_delivery,
    resolve_stale,
    save_situation,
)
from packages.core.store import get_event
from packages.core.tickets import create_ticket, ticket_for_situation
from packages.core.tokens import issue_token
from packages.core.triage import run_triage
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


def _target_specs(profile: Profile) -> list[dict[str, Any]]:
    """Every (pattern, params) this company can address, newest shape first.

    `moves.targets` is per-source, declared by each connector. The older
    single `target_url_pattern` is still honoured so a profile version stored
    before connectors declared their own capability keeps working — those rows
    are the system of record and are never rewritten.
    """
    targets = profile.moves.get("targets") or {}
    specs = [
        {"pattern": t.get("pattern"), "params": t.get("params") or []}
        for t in targets.values()
        if t.get("pattern")
    ]
    legacy = profile.moves.get("target_url_pattern")
    if legacy:
        specs.append({"pattern": legacy, "params": profile.moves.get("target_params") or []})
    return specs


def extract_target(profile: Profile, url: str | None) -> dict[str, str] | None:
    """Map an evidence URL onto named action params.

    Tries each connected source's pattern and takes the one that matches — a
    GitHub issue URL only matches GitHub's shape — so several connectors can
    declare writes without competing for one global pattern.
    """
    if not url:
        return None
    for spec in _target_specs(profile):
        if not spec["params"]:
            continue
        match = re.search(str(spec["pattern"]), url)
        if match:
            return dict(zip(spec["params"], match.groups(), strict=False))
    return None


def _fill(value: Any, values: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return value.format(**values) if "{" in value else value
    if isinstance(value, dict):
        return {k: _fill(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, values) for v in value]
    return value


def params_for_move(
    profile: Profile, move: str, argument: str, situation: Situation,
    default_target: dict[str, Any] | None = None,
) -> dict | None:
    """Build the concrete action params a move needs, from its profile spec.

    ``default_target`` is the fallback address for a move whose destination the
    situation itself can't supply — a CREATE move fired from a Slack incident
    has no GitHub URL to derive a repo from, so the connected repo is passed in
    (see _home_target). This is what lets one app's signal open work in another.
    """
    spec = (profile.moves.get("registry") or {}).get(move)
    if spec is None:
        return None
    values: dict[str, Any] = {"argument": argument, "situation_id": situation.id}

    template = dict(spec.get("params") or {})
    needs_target = template.pop("target", None)
    if needs_target:
        url = situation.evidence[0].url if situation.evidence else None
        # Some moves only work on ONE kind of record behind an otherwise
        # identical URL — approving is valid on a pull request and meaningless
        # on an issue. The connector declares the pattern; returning None here
        # means the move is simply not offered for this situation, which is the
        # same path an unresolvable target already takes. Failing at build time
        # beats failing at the API, where a person has already approved it.
        applies_to = spec.get("applies_to_url")
        if applies_to and not (url and re.search(str(applies_to), url)):
            return None
        # The situation's own address first; the connected tool's home second
        # (cross-app create). Only one of them needs to resolve.
        target = extract_target(profile, url) or default_target
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


async def _home_target(session: AsyncSession, profile: Profile, move: str) -> dict | None:
    """Where a CREATE move lands when the situation has no address of its own —
    the connected tool's home. A GitHub create_issue opens in the repo the
    workspace connected; the value lives in that source's connection config, so
    it is workspace DATA, not something the engine hardcodes to a tool."""
    spec = (profile.moves.get("registry") or {}).get(move, {})
    source = (spec.get("auth") or {}).get("source")
    if not source:
        return None
    cred = await get_credential(session, profile.company_id, source)
    config = cred[1] if cred else {}
    return config or None


async def learned_arguments(
    session: AsyncSession, profile: Profile, move: str, limit: int = 12
) -> list[str]:
    """The real values this company uses for a move's argument, most-used first.

    Empty when the move takes no vocabulary-backed argument, or when nothing has
    been observed yet — and empty must stay empty. Suggesting a label a repo has
    never used would silently create it on the next apply.
    """
    spec = (profile.moves.get("registry") or {}).get(move, {})
    source_spec = spec.get("argument_values")
    if not isinstance(source_spec, dict) or not source_spec.get("field"):
        return []
    values = await observed_values(
        session,
        profile.company_id,
        str(source_spec["field"]),
        name_key=source_spec.get("name_key"),
        source=(spec.get("auth") or {}).get("source"),
    )
    return [v["value"] for v in values[:limit]]


async def default_argument(
    session: AsyncSession, profile: Profile, move: str, situation: Situation
) -> str:
    """What a one-click button means for a move.

    For a move whose argument is drawn from a real vocabulary (a label, an
    assignee), the default is what this company ACTUALLY uses most — learned
    from ingested events, never declared. If nothing has been observed, the
    answer is "" and the caller declines to build the action, which is the
    honest outcome: we do not know a safe value, so we do not invent one.
    """
    spec = (profile.moves.get("registry") or {}).get(move, {})
    if learned := await learned_arguments(session, profile, move, limit=1):
        return learned[0]
    if spec.get("argument_values"):
        return ""  # vocabulary-backed, but we have never seen a value
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


def _notify_route(routing: dict, roster: list[dict], assignee: str | None) -> dict:
    """Widen every route to reach the assignee AND everyone who owns the
    outcome — the workspace owner and any leads. This is the "email the owner,
    the assigned person, and the leads" of a live assignment, resolved from the
    roster's roles rather than hardcoded anywhere."""
    extra: set[str] = set()
    for member in roster:
        email = member.get("email")
        if not email:
            continue
        if member.get("id") == assignee:
            extra.add(email)
        if any(r in member.get("roles", []) for r in ("owner", "lead")):
            extra.add(email)
    routes = {
        sev: {**r, "recipients": sorted(set(r.get("recipients", [])) | extra)}
        for sev, r in routing["routes"].items()
    }
    return {**routing, "routes": routes}


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


async def auto_assign(
    session: AsyncSession, profile: Profile, situation: Situation, roster: list[dict]
) -> dict:
    """Live-mode assignment: don't just propose a link — pick the best free,
    skill-matched person (the same skill-aware choose_assignee the manual path
    uses) and actually assign them. Only acts when the AI is confident enough
    (autonomy.min_confidence); an unsure call is left for a person rather than
    guessing. This is the heart of "the system does the work while you're away".
    """
    team = profile.moves.get("team", {}) or {}
    if not team.get("assign_move"):
        return {"status": "no_assign_move", "assignee": None}
    if await _current_assignee(session, profile, situation):
        return {"status": "already_owned", "assignee": None}
    url = situation.evidence[0].url if situation.evidence else None
    target = extract_target(profile, url)
    if target is None or not roster:
        return {"status": "no_target" if target is None else "no_team", "assignee": None}

    workload = await open_workload(session, profile.company_id, team.get("workload", {}))
    candidates = free_members(profile, roster, workload)
    if not candidates:
        return {"status": "nobody_free", "assignee": None}

    prompts = profile.vocabulary.get("prompts", {})
    decision = choose_assignee(situation, candidates, prompts.get("choose_assignee", "{title}"))
    autonomy = profile.moves.get("autonomy", {}) or {}
    if decision.assignee == "none" or decision.confidence < float(autonomy.get("min_confidence", 0.7)):
        # not sure who fits — leave it for a person, don't assign the wrong one
        return {
            "status": "unsure", "assignee": None,
            "recommended": decision.assignee, "confidence": round(decision.confidence, 2),
        }

    payload = {
        "situation_id": situation.id, "assignee": decision.assignee, "target": target,
        "item_label": " ".join(str(v) for v in target.values()),
        "event_id": situation.evidence[0].event_id if situation.evidence else None,
        "title": situation.title, "summary": situation.summary,
    }
    result = await accept_assignment(session, profile, payload, clicked_by="ai")
    return {
        "status": "assigned", "assignee": decision.assignee,
        "confidence": round(decision.confidence, 2), "rationale": decision.rationale,
        "assign_status": result.get("assign_status"), "ticket_id": result.get("ticket_id"),
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
    # A consumed assignment token (a person clicked the link) or the AI's own
    # confident pick in Live is already the approval — it runs for real, not a
    # rehearsal, the same reframe manual approvals follow.
    result: ActionResult = await act(
        {"session": session}, request, profile.moves.get("registry", {}),
        {**policy, "force_approval": False, "pre_approved": True},
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
    if action_name == "escalate":
        params = None
    else:
        # cross-app: a create move fired from (say) a Slack incident lands in the
        # connected tool's home when the situation has no address of its own
        home = await _home_target(session, profile, action_name)
        params = params_for_move(profile, action_name, decision.argument, situation, default_target=home)

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
    # Reached only in Live mode (see evaluate_watchers), so this acts for real
    # and may touch public tools on its own — that is what "the system does the
    # work while you're away" means. The confidence/severity gate (`human`)
    # still routes the genuinely uncertain ones to a person instead of guessing.
    #
    # When the AI is NOT deferring to a human, the action is one the operator
    # ALLOWLISTED for autonomy and the AI cleared the confidence bar — that
    # combination IS the pre-approval, so it executes rather than sitting in the
    # queue. Without this, a confident allowlisted action still hit the default
    # "require approval" gate and waited — the opposite of acting on its own.
    result: ActionResult = await act(
        {"session": session}, request, profile.moves.get("registry", {}),
        {**policy, "force_approval": human, "pre_approved": not human,
         "dry_run": False, "allow_public_actions": True},
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
    # Universal inbound triage: judge fresh records from EVERY connected source
    # and raise a situation for anything that needs a person. Merged into the
    # same list so it gets the identical assign / notify / autonomy treatment a
    # watcher's situation gets — one path, every source.
    situations = situations + await run_triage(session, profile)

    policy = await approval_policy(session, profile)
    roster = await team_roster(session, company_id)
    routing = routing_config(profile, policy["dry_run"], roster)

    live = not policy["dry_run"]
    delivered = 0
    assignments: list[dict] = []
    for situation in situations:
        await save_situation(session, situation)

        # Live: actually assign the best-matched free person. Practice: only
        # propose a one-click link and wait for a person.
        if live:
            outcome = await auto_assign(session, profile, situation, roster)
        else:
            outcome = {"status": "practice"}
        if outcome.get("status") == "assigned":
            proposal = {
                "assignee": outcome["assignee"], "links": [],
                "note": f"Auto-assigned to {outcome['assignee']} — {outcome.get('rationale', '')}",
                "status": "auto_assigned",
            }
        else:
            proposal = await propose_assignment(session, profile, situation, roster)
        assignments.append(
            {"situation_id": situation.id, **{k: v for k, v in proposal.items() if k != "links"}}
        )

        brief: Brief = assemble_brief(situation)
        brief.assigned_to = proposal.get("assignee")
        brief.action_links = proposal.get("links", [])
        brief.note = proposal.get("note", "")
        brief.reply_ref = f"{company_id}:{situation.id}"
        # A live assignment reaches the assignee AND the people who own the
        # outcome (owner + leads); practice keeps the plain notify list.
        route = _notify_route(routing, roster, proposal.get("assignee")) if live and proposal.get("assignee") else routing
        receipt = deliver(brief, route)
        await record_delivery(session, receipt)
        delivered += 1

    resolved = await resolve_stale(session, company_id, [s.id for s in situations], kind="business")
    await session.commit()
    await publish_feed_update(company_id, len(situations))

    autonomy = profile.moves.get("autonomy", {}) or {}
    # Live mode IS the autonomy switch. When the operator has gone live, the AI
    # acts on its own — triage, assign, notify — while nobody is watching.
    # Practice keeps it hands-off: it proposes, a person disposes. `enabled`
    # stays a hard off-switch for a workspace that wants to remain manual even
    # when live, defaulting on so going live just works.
    live = not policy.get("dry_run", True)
    decisions: list[dict] = []
    if live and autonomy.get("enabled", True) and autonomy.get("allowed_actions"):
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

    # Reviewers run on the slower analyze cadence (not the 5-min watcher cron):
    # each is an LLM fan-out per open record, gated by the record_reviews ledger
    # so an unchanged PR is never re-paid for. Findings surface as ordinary
    # situations; nothing posts to a real tool here.
    reviews = await run_reviewers(
        session,
        company_id,
        profile.reviewers,
        status_field=(profile.things or {}).get("status_field"),
        activity_field=(profile.things or {}).get("activity_field"),
    )
    await session.commit()

    return {
        "norms": len(norms),
        "clarifications": len(clarifications),
        "reviews": reviews,
        **watcher_summary,
    }


# ------------------------------ PR review posting ------------------------------

_SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


async def _pr_review_findings(
    session: AsyncSession, company_id: str, thing_id: str
) -> list[Situation]:
    """The open review findings for one record, most-severe first. Keyed on the
    finding id shape (``review:{reviewer}:{thing}:{concern}:{hash}``), so it
    picks up every concern's findings on this PR and nothing else."""
    rows = await session.execute(
        text(
            f"""
            SELECT {situations._COLUMNS} FROM situations
            WHERE company_id = :c AND status <> 'resolved'
              AND id LIKE 'review:%:' || :tid || ':%'
            ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                                   WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END
            """
        ),
        {"c": company_id, "tid": thing_id},
    )
    return [situations._situation_from_row(r) for r in rows]


def _render_comment(finding: Situation) -> str:
    concern = finding.rule.split(".")[-1]
    head = f"**[{finding.severity.upper()} · {concern}] {finding.title}**"
    parts = [head]
    if finding.summary:
        parts.append(finding.summary)
    return "\n\n".join(parts)


def assemble_pr_review(
    profile: Profile, move: str, findings: list[Situation]
) -> tuple[str, list[dict]]:
    """Turn a PR's findings into ONE review: a grouped summary body plus an
    inline comment for every anchored finding. The inline-comment field names
    come from the move's declared ``review_comment`` shape, so nothing here
    knows GitHub calls them path/line/side — the same data-driven discipline the
    move params already follow.

    A finding with no file/line can't be pinned to the diff, so it folds into
    the summary instead of being dropped — the review still says it."""
    spec = ((profile.moves.get("registry") or {}).get(move) or {}).get("review_comment") or {}
    path_key = spec.get("path_key", "path")
    line_key = spec.get("line_key", "line")
    body_key = spec.get("body_key", "body")
    side = spec.get("side", "RIGHT")

    comments: list[dict] = []
    unanchored: list[Situation] = []
    for f in findings:
        line = f.line_end or f.line_start
        if f.file_path and line:
            comments.append({
                path_key: f.file_path, line_key: int(line), "side": side,
                body_key: _render_comment(f),
            })
        else:
            unanchored.append(f)

    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    tally = ", ".join(
        f"{counts[s]} {s}" for s in sorted(counts, key=lambda s: _SEV_RANK.get(s, 9))
    )
    lines = [f"AI review found {len(findings)} issue(s) ({tally}). See inline comments."]
    for f in unanchored:
        lines.append(f"- {f.title} — {f.summary}".strip())
    return "\n".join(lines), comments


def reviewable_types(profile: Profile) -> list[str]:
    """The record types this profile's reviewers target — so the UI can offer a
    Review button only where a reviewer actually exists, and on nothing else."""
    types = []
    for reviewer in profile.reviewers or []:
        t = (reviewer.get("select") or {}).get("thing_type")
        if t and t not in types:
            types.append(t)
    return types


async def review_one_record(
    session: AsyncSession, profile: Profile, thing_id: str, force: bool = True
) -> dict | None:
    """Run the reviewer over ONE record on demand — what a Review button calls.
    Returns None when the record is unknown or no reviewer targets its type."""
    event = await get_event(session, profile.company_id, thing_id)
    if event is None:
        return None
    reviewer = next(
        (r for r in (profile.reviewers or [])
         if (r.get("select") or {}).get("thing_type") == event.type),
        None,
    )
    if reviewer is None:
        return None
    content = (event.content or "").strip()
    thing = {
        "id": event.id, "company_id": event.company_id, "source": event.source,
        "type": event.type, "content": content,
        "title": content.splitlines()[0][:120] if content else event.id,
        "metadata": event.metadata or {}, "timestamp": event.timestamp,
    }
    return await review_thing(
        session, profile.company_id, reviewer, thing,
        (profile.things or {}).get("activity_field"), force=force,
    )


async def review_findings(
    session: AsyncSession, company_id: str, thing_id: str
) -> list[dict]:
    """This record's open findings, shaped for the review UI — one entry per
    finding with its exact spot, concern, severity and the fix. Most-severe
    first (via _pr_review_findings)."""
    out = []
    for s in await _pr_review_findings(session, company_id, thing_id):
        out.append({
            "id": s.id,
            "concern": s.rule.split(".")[-1],
            "severity": s.severity,
            "category": s.category,
            "file_path": s.file_path,
            "line": s.line_end or s.line_start,
            "title": s.title.split(" (")[0],  # drop the "(file:line)" the title carries for the feed
            "rationale": s.summary,
            "suggestion": s.recommended_action,
            "confidence": s.confidence,
        })
    return out


async def request_pr_review(
    session: AsyncSession, profile: Profile, thing_id: str, requested_by: str = "ui"
) -> ActionResult | None:
    """Assemble a PR's review findings into one inline REQUEST_CHANGES review and
    put it through the SAME gate every write uses. Returns None when there is
    nothing open to post.

    The move is `public`, so act() queues it for a person regardless of
    confidence — the gate stays closed, exactly as promised. Approving it runs
    it (and Practice mode still turns that into a rehearsal, not a real post).
    A stable synthetic situation_id per record makes act()'s dedup give one
    pending review per PR, not a fresh ask every time the button is pressed."""
    findings = await _pr_review_findings(session, profile.company_id, thing_id)
    if not findings:
        return None

    move = "request_changes_on_pull_request"
    for reviewer in profile.reviewers or []:
        if reviewer.get("post", {}).get("move"):
            move = reviewer["post"]["move"]
            break

    url = next((f.evidence[0].url for f in findings if f.evidence), None)
    target = extract_target(profile, url)
    if target is None:
        return None

    summary, comments = assemble_pr_review(profile, move, findings)
    params = {
        **target,
        "body": {"event": "REQUEST_CHANGES", "body": summary, "comments": comments},
    }
    return await request_action(
        session, profile, move, params,
        situation_id=f"pr-review:{thing_id}",
        requested_by=requested_by,
        pre_approved=False,  # a public write always reaches a person
    )


async def request_action(
    session: AsyncSession,
    profile: Profile,
    action: str,
    params: dict,
    situation_id: str | None = None,
    requested_by: str = "system",
    pre_approved: bool = True,
    force_approval: bool = False,
    force_live: bool = False,
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

    ``force_live`` executes for real even while the workspace is in Practice
    mode — the ONE caller that sets it is a saved workflow, because building
    and enabling a workflow is a standing decision to automate, and a workflow
    that only rehearsed would never actually do its job. This is the single
    deliberate hole in the Practice-mode promise; nothing else may open it.
    """
    if situation_id:
        situation = await get_situation(session, profile.company_id, situation_id)
        if situation is not None:
            argument = str(
                params.get("argument")
                or await default_argument(session, profile, action, situation)
            )
            if not argument and (
                (profile.moves.get("registry") or {}).get(action, {}).get("argument_values")
            ):
                # a vocabulary-backed move with nothing learned yet: refuse
                # rather than send a made-up value to a real system
                return ActionResult(
                    action=action,
                    status="failed",
                    detail=(
                        f"I don't know a safe value for {action} yet — "
                        "nothing in your history shows which one you use."
                    ),
                )
            home = await _home_target(session, profile, action)
            derived = params_for_move(profile, action, argument, situation, default_target=home)
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
    effective = {
        **policy,
        "pre_approved": pre_approved,
        "force_approval": force_approval or policy.get("force_approval", False),
    }
    # A human clicking a specific button IS the approval, and it runs for real —
    # Practice mode never turns your own click into a rehearsal. Only the agent's
    # un-approved typed intents (pre_approved=False) still wait for a person.
    # `force_live` remains for saved workflows, which are their own standing yes.
    if force_live or pre_approved:
        effective["dry_run"] = False
    result = await act(
        {"session": session}, request, profile.moves.get("registry", {}), effective,
    )
    await session.commit()
    return result




