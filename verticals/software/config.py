from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncSession

from packages.connectors.github import repo_slug
from packages.core.credentials import get_credential
from packages.core.settings import get_setting, set_setting

# The software vertical's domain config. The core knows nothing about
# github/slack/zendesk — the vertical declares sources, field mappings, graph
# schema, entity rules, norms, detection rules, prompts and actions as DATA.

VERTICAL = "software"
COMPANY_ID = "default"

SOURCES: list[str] = ["github", "slack", "zendesk"]


# ==========================================================================
# 1. Connector configs — which tools, and how their fields map into Event
# ==========================================================================


def _github_source_config(repo: str, company_id: str = COMPANY_ID) -> dict:
    return {
        "source": "github",
        "company_id": company_id,
        "context": {"repo": repo_slug(repo)},
        "mapping": {
            "id": {"template": "gh-{repo}-{number}"},
            "type": {
                "when_exists": "pull_request",
                "then": {"const": "pull_request"},
                "else": {"const": "issue"},
            },
            "actor_id": {"path": "user.login", "default": "unknown"},
            "actor_name": {"path": "user.login", "default": "unknown"},
            "actor_email": {"path": "user.email"},
            "timestamp": {"path": "created_at"},
            "content": {
                "concat": [
                    {"path": "title", "default": ""},
                    {"const": "\n\n"},
                    {"path": "body", "default": ""},
                ]
            },
            "metadata": {
                "number": {"path": "number"},
                "state": {"path": "state"},
                "url": {"path": "html_url"},
                "labels": {"path": "labels", "default": []},
                "assignee": {"path": "assignee.login"},
                "comments": {"path": "comments", "default": 0},
                "created_at": {"path": "created_at"},
                "updated_at": {"path": "updated_at"},
                "closed_at": {"path": "closed_at"},
                "merged_at": {"path": "_merged_at"},
                "repo": {"path": "_repo"},
            },
        },
    }


def _slack_source_config(channel: str, company_id: str = COMPANY_ID) -> dict:
    return {
        "source": "slack",
        "company_id": company_id,
        "context": {"channel": channel},
        "mapping": {
            "id": {"template": "slack-{_channel_id}-{ts}"},
            "type": {"const": "message"},
            "actor_id": {"path": "user", "default": "unknown"},
            "actor_name": {"path": "user", "default": "unknown"},
            "timestamp": {"epoch": "ts"},
            "content": {"path": "text", "default": ""},
            "metadata": {
                "channel": {"path": "_channel"},
                "ts": {"path": "ts"},
                "thread_ts": {"path": "thread_ts"},
                "reactions": {"path": "reactions", "default": []},
            },
        },
    }


def _zendesk_source_config(subdomain: str, company_id: str = COMPANY_ID) -> dict:
    return {
        "source": "zendesk",
        "company_id": company_id,
        "context": {"subdomain": subdomain},
        "mapping": {
            "id": {"template": "zd-{_subdomain}-{id}"},
            "type": {"const": "ticket"},
            "actor_id": {"path": "requester_id", "default": "unknown"},
            "actor_name": {"path": "requester_id", "default": "unknown"},
            "timestamp": {"path": "created_at"},
            "content": {
                "concat": [
                    {"path": "subject", "default": ""},
                    {"const": "\n\n"},
                    {"path": "description", "default": ""},
                ]
            },
            "metadata": {
                "ticket_id": {"path": "id"},
                "status": {"path": "status"},
                "priority": {"path": "priority"},
                "tags": {"path": "tags", "default": []},
                "assignee_id": {"path": "assignee_id"},
                "created_at": {"path": "created_at"},
                "resolved_at": {"path": "_resolved_at"},
                "url": {"template": "https://{subdomain}.zendesk.com/agent/tickets/{id}"},
            },
        },
    }


async def connector_config(session: AsyncSession, company_id: str = COMPANY_ID) -> list[dict]:
    """Build connector specs from the encrypted credential store (UI-connected),
    falling back to GITHUB_REPO env for headless dev."""
    specs: list[dict] = []

    github = await get_credential(session, company_id, "github")
    if github and github[1].get("repo"):
        token, cfg = github
        specs.append(
            {
                "type": "github",
                "repo": cfg["repo"],
                "token": token or None,
                "limit": int(cfg.get("limit", 30)),
                "source_config": _github_source_config(cfg["repo"], company_id),
            }
        )
    elif os.getenv("GITHUB_REPO"):
        repo = os.environ["GITHUB_REPO"]
        specs.append(
            {
                "type": "github",
                "repo": repo,
                "token": os.getenv("GITHUB_TOKEN") or None,
                "limit": int(os.getenv("GITHUB_LIMIT", "30")),
                "source_config": _github_source_config(repo, company_id),
            }
        )

    slack = await get_credential(session, company_id, "slack")
    if slack and slack[1].get("channel"):
        token, cfg = slack
        specs.append(
            {
                "type": "slack",
                "token": token,
                "channel": cfg["channel"],
                "limit": int(cfg.get("limit", 30)),
                "source_config": _slack_source_config(cfg["channel"], company_id),
            }
        )

    zendesk = await get_credential(session, company_id, "zendesk")
    if zendesk and zendesk[1].get("subdomain"):
        token, cfg = zendesk
        specs.append(
            {
                "type": "zendesk",
                "token": token,
                "subdomain": cfg["subdomain"],
                "limit": int(cfg.get("limit", 30)),
                "source_config": _zendesk_source_config(cfg["subdomain"], company_id),
            }
        )

    return specs


# ==========================================================================
# 2. Graph schema
# ==========================================================================
GRAPH_SCHEMA: dict = {
    "node_types": ["Incident", "PullRequest", "Ticket", "Feature", "Person", "Message"],
    "edge_types": ["AUTHORED", "CLOSES", "IMPLEMENTS", "MENTIONS", "SAME_AS", "CAUSED_BY"],
}


# ==========================================================================
# 3. Entity resolution rules
# ==========================================================================
ENTITY_RULES: dict = {
    "node_types": {
        "github": {"issue": "Incident", "pull_request": "PullRequest"},
        "zendesk": {"ticket": "Ticket"},
        "slack": {"message": "Message"},
    },
    "default_node_type": "Event",
    "id_patterns": [r"#(\d+)", r"\b[A-Z]{3,}-\d+\b"],
    "similarity_hard": 0.68,
    "similarity_soft": 0.63,
    "candidate_limit": 25,
    "keyword_rules": [
        {
            "name": "checkout-incident",
            "any": ["checkout", "pay button", "discount code", "charged", "gift card",
                    "promo code", "payment failed", "failed at checkout", "order confirmation"],
        }
    ],
}


# ==========================================================================
# 4. Norm definitions
# ==========================================================================
NORM_DEFINITIONS: list[dict] = [
    {"name": "issue_resolution_hours", "unit": "hours", "window_days": 365,
     "source": "github", "type": "issue", "end_field": "closed_at"},
    {"name": "pr_review_hours", "unit": "hours", "window_days": 365,
     "source": "github", "type": "pull_request", "end_field": "merged_at"},
    {"name": "ticket_resolution_hours", "unit": "hours", "window_days": 365,
     "source": "zendesk", "type": "ticket", "end_field": "resolved_at"},
]


# ==========================================================================
# 5. Detection rules — what counts as a "situation" for a software company
# ==========================================================================
DETECTION_RULES: list[dict] = [
    {
        "name": "p0_no_owner",
        "title": "P0 issue with no owner",
        "severity": "critical",
        "select": {
            "source": "github", "type": "issue",
            "where": [
                {"field": "metadata.state", "op": "eq", "value": "open"},
                {"field": "metadata.labels", "op": "contains_any",
                 "value": ["p0", "critical", "urgent", "sev1", "blocker"]},
                {"field": "metadata.assignee", "op": "is_null"},
            ],
        },
        "llm": True, "prompt": "p0_no_owner", "limit": 3,
    },
    {
        "name": "unassigned_bug",
        "title": "Unassigned bug report",
        "severity": "high",
        "select": {
            "source": "github", "type": "issue",
            "where": [
                {"field": "metadata.state", "op": "eq", "value": "open"},
                {"field": "metadata.labels", "op": "contains_any", "value": ["bug", "defect", "regression"]},
                {"field": "metadata.assignee", "op": "is_null"},
            ],
        },
        "llm": True, "prompt": "unassigned_bug", "limit": 3,
    },
    {
        # The assignment workflow trigger: open work that nobody owns.
        "name": "needs_owner",
        "title": "Open issue with no owner",
        "severity": "high",
        "select": {
            "source": "github", "type": "issue",
            "where": [
                {"field": "metadata.state", "op": "eq", "value": "open"},
                {"field": "metadata.assignee", "op": "is_null"},
            ],
        },
        "llm": True, "prompt": "needs_owner", "limit": 5,
    },
    {
        # Untriaged work: open and never labelled — nobody has classified it,
        # whether or not somebody is assigned. The two rules above already cover
        # *labelled* unowned issues, so requiring no labels means an issue is
        # never flagged twice. Closing or labelling the issue retires the flag.
        "name": "untriaged_issue",
        "title": "Open issue with no triage",
        "severity": "medium",
        "select": {
            "source": "github", "type": "issue",
            "where": [
                {"field": "metadata.state", "op": "eq", "value": "open"},
                # an OWNED but unlabelled issue. Unowned work is `needs_owner`'s
                # job — without this the PM gets two emails for one issue.
                {"field": "metadata.assignee", "op": "not_null"},
                {"field": "metadata.labels", "op": "is_null"},
            ],
        },
        "llm": True, "prompt": "untriaged_issue", "limit": 5,
    },
    {
        "name": "sla_breach_risk",
        "title": "Issue open far longer than normal",
        "severity": "high",
        "select": {
            "source": "github", "type": "issue",
            "where": [{"field": "metadata.state", "op": "eq", "value": "open"}],
        },
        "norm": {"metric": "issue_resolution_hours", "k": 2.0},
        "llm": True, "prompt": "sla_breach_risk", "limit": 3,
    },
    {
        "name": "stale_open_issue",
        "title": "Stale open issue",
        "severity": "medium",
        "recommended_action": "Triage or close: no activity in over 30 days.",
        "select": {
            "source": "github", "type": "issue",
            "where": [
                {"field": "metadata.state", "op": "eq", "value": "open"},
                {"field": "timestamp", "op": "older_than_days", "value": 30},
            ],
        },
        "llm": False, "limit": 5,
    },
    {
        "name": "spec_drift",
        "title": "Pull request closes no tracked issue",
        "severity": "medium",
        "graph": {"node_type": "PullRequest", "missing_edge_type": "CLOSES"},
        "llm": True, "prompt": "spec_drift", "limit": 3,
    },
]


# ==========================================================================
# 6. LLM prompts — severity classification + brief assembly
# ==========================================================================
LLM_PROMPTS: dict[str, str] = {
    "p0_no_owner": (
        "You triage engineering incidents for a software company.\n"
        "A P0-labelled GitHub issue has no assignee.\n\n"
        "Title rule: {title}\nSource: {source}\nAuthor: {actor}\n"
        "Issue content:\n{content}\n\nMetadata: {metadata}\nTeam norms: {norms}\n\n"
        "Classify the severity, write a two-sentence summary of the risk, and give one "
        "concrete recommended action naming who should pick it up."
    ),
    "unassigned_bug": (
        "You triage bug reports for a software company.\n"
        "An open bug has no assignee.\n\nSource: {source}\nAuthor: {actor}\n"
        "Issue content:\n{content}\n\nMetadata: {metadata}\nTeam norms: {norms}\n\n"
        "Classify severity from real user impact, summarize in two sentences, and recommend one action."
    ),
    "untriaged_issue": (
        "You triage the inbound issue queue for a software team.\n"
        "This GitHub issue is open and has never been labelled — nobody has classified it.\n\n"
        "Source: {source}\nAuthor: {actor}\nIssue content:\n{content}\n\n"
        "Metadata: {metadata}\nTeam norms: {norms}\n\n"
        "Judge severity from the real user impact described (a broken API or crash is "
        "high; a feature request or chore is low). Summarize the risk in two sentences. "
        "Recommend one concrete next step: the label to apply, and who should own it "
        "(note the assignee in the metadata if one is already set)."
    ),
    "sla_breach_risk": (
        "You monitor delivery risk for a software company.\n"
        "This issue has been open far longer than the team's normal resolution time.\n\n"
        "Source: {source}\nAuthor: {actor}\nIssue content:\n{content}\n\n"
        "Metadata: {metadata}\nTeam norms (baselines): {norms}\n\n"
        "Judge how serious the delay is, summarize in two sentences citing the norm, "
        "and recommend one action."
    ),
    "needs_owner": (
        "You triage the inbound issue queue for a software team.\n"
        "This GitHub issue is open and nobody has been assigned to it.\n\n"
        "Source: {source}\nAuthor: {actor}\nIssue content:\n{content}\n\n"
        "Metadata: {metadata}\nTeam norms: {norms}\n\n"
        "Judge severity from the real user impact described. Summarize the problem in two "
        "sentences so a project manager can decide who should pick it up. Recommend what "
        "kind of engineer should own it."
    ),
    # Who should own this work? Only people the vertical says are free are offered.
    "choose_assignee": (
        "You assign engineering work for a software team.\n"
        "A situation needs an owner. Pick the single best person from the AVAILABLE "
        "engineers below — they all have spare capacity right now.\n\n"
        "Situation: {title}\nSeverity: {severity}\nSummary: {summary}\n\n"
        "Evidence:\n{evidence}\n\n"
        "Available engineers (id, name, roles, skills, current workload / capacity):\n"
        "{candidates}\n\n"
        "Match the work to skills, and prefer whoever has the lightest workload. "
        "Answer with that person's id. Answer 'none' only if nobody listed can "
        "plausibly do this work.\n"
        "Set confidence between 0 and 1."
    ),
    # The agent's own decision prompt: which registered action closes this out?
    "choose_action": (
        "You are the autonomous operator of a software company's engineering workspace.\n"
        "A situation has been detected. Decide the single best action to resolve it.\n\n"
        "Situation: {title}\nSeverity: {severity}\nSummary: {summary}\n"
        "Prior recommendation: {recommended_action}\n\nEvidence:\n{evidence}\n\n"
        "You may choose exactly one of these actions: {actions}\n\n"
        "Meaning of each action and what `argument` must contain:\n"
        "  apply_label   -> argument is ONE GitHub label name (e.g. bug, enhancement, question)\n"
        "  assign_issue  -> argument is ONE GitHub username to assign\n"
        "  draft_reply   -> argument is the reply text\n"
        "  comment_on_pr -> argument is comment text posted on the EXISTING issue/PR "
        "(public, needs human approval). Never propose opening a new issue.\n"
        "  page_engineer -> argument is why the on-call must wake up (needs human approval)\n\n"
        "Prefer the smallest reversible action that moves the work forward. Labelling an "
        "untriaged issue is usually correct. Choose 'escalate' ONLY if a human judgement "
        "call is genuinely required and no action above is safe.\n"
        "Set confidence between 0 and 1: how sure you are this is the right action."
    ),
    "spec_drift": (
        "You audit engineering traceability.\n"
        "This pull request is not linked to any tracked issue it closes, so shipped work "
        "may not match a spec.\n\nPR content:\n{content}\n\nMetadata: {metadata}\n\n"
        "Judge the risk, summarize in two sentences, and recommend one action."
    ),
}


# ==========================================================================
# 7. Routing + action registry
# ==========================================================================
# --------------------------------------------------------------------------
# Team roster. Managed from the UI (Team tab) and stored per-company in the
# generic `settings` store. The list below is only the seed used before anyone
# has configured a team. `roles` drives who gets emailed and who can be given
# work; `max_open_issues` is how much work a person holds before they're busy.
# --------------------------------------------------------------------------
TEAM_KEY = "team_roster"

TEAM_ROLES: list[str] = ["project_manager", "team_lead", "engineer"]

TEAM_ROSTER_DEFAULT: list[dict] = []

# Everyone holding one of these roles is emailed when a situation is raised.
NOTIFY_ROLES: list[str] = ["project_manager", "team_lead"]

# Only people with this role can be given the work.
ASSIGNABLE_ROLE = "engineer"


async def team_roster(session: AsyncSession, company_id: str = COMPANY_ID) -> list[dict]:
    """The team as configured in the UI, falling back to the seed."""
    roster = await get_setting(session, company_id, TEAM_KEY, None)
    return list(roster) if roster else list(TEAM_ROSTER_DEFAULT)


async def save_team_roster(
    session: AsyncSession, roster: list[dict], company_id: str = COMPANY_ID
) -> None:
    await set_setting(session, company_id, TEAM_KEY, roster)

# How the core counts who is currently busy (generic query, vertical semantics).
WORKLOAD_SPEC: dict = {
    "source": "github",
    "type": "issue",
    "state_field": "state",
    "state_value": "open",
    "assignee_field": "assignee",
}


def recipients_for(roles: list[str], roster: list[dict]) -> list[str]:
    return [
        m["email"]
        for m in roster
        if m.get("email") and any(r in m.get("roles", []) for r in roles)
    ]


def routing_config(dry_run: bool, roster: list[dict]) -> dict:
    """Every raised situation emails the PM + team lead. dry_run composes it
    without sending, exactly like actions."""
    to = recipients_for(NOTIFY_ROLES, roster)
    email_route = {"channel": "email", "recipients": to}
    return {
        "from": os.getenv("SMTP_FROM", "ai-os@localhost"),
        "dry_run": dry_run,
        "routes": {
            "critical": email_route,
            "high": email_route,
            "medium": email_route,
            "low": email_route,
        },
        "default": {"channel": "console", "recipients": to},
    }

ACTION_REGISTRY: dict = {
    # --- low-risk, reversible: the agent may run these on its own ---
    "apply_label": {
        "kind": "http", "approval_required": False,
        "method": "POST", "url": "https://api.github.com/repos/{repo}/issues/{number}/labels",
        "auth": {"source": "github"},
    },
    "assign_issue": {
        "kind": "http", "approval_required": False,
        "method": "POST", "url": "https://api.github.com/repos/{repo}/issues/{number}/assignees",
        "auth": {"source": "github"},
    },
    "draft_reply": {
        "kind": "log", "approval_required": False,
        "template": "DRAFT reply prepared for {situation_id}",
    },
    "close_issue": {
        "kind": "http", "approval_required": False,
        "method": "PATCH", "url": "https://api.github.com/repos/{repo}/issues/{number}",
        "auth": {"source": "github"},
    },
    # --- disruptive or public: a human always decides ---
    # posts on the EXISTING issue or PR (GitHub's comments endpoint covers
    # both). There is deliberately no "open a new issue" action: every
    # situation already has a source issue, and a second one would collide.
    "comment_on_pr": {
        "kind": "http", "approval_required": True,
        "method": "POST", "url": "https://api.github.com/repos/{repo}/issues/{number}/comments",
        "auth": {"source": "github"},
    },
    "page_engineer": {
        "kind": "log", "approval_required": True,
        "template": "PAGE on-call engineer about {situation_id}",
    },
}

# SAFETY: dry_run means external calls are recorded, not sent.
# This is the DEFAULT (used before anyone touches the toggle). The live value is
# stored per-company in `settings` and read at call time, so the operator can
# turn writes on/off from the UI and it persists while they are away.
APPROVAL_DEFAULTS: dict = {"default_require_approval": False, "dry_run": True}

DRY_RUN_KEY = "dry_run"


async def approval_policy(session: AsyncSession, company_id: str = COMPANY_ID) -> dict:
    """The approval policy in force right now, honoring the operator's toggle."""
    dry_run = await get_setting(session, company_id, DRY_RUN_KEY, APPROVAL_DEFAULTS["dry_run"])
    return {**APPROVAL_DEFAULTS, "dry_run": bool(dry_run)}


# ==========================================================================
# 9. Autonomy policy — when may the agent act alone?
#    The AI runs the loop by default. A human is pulled in only when:
#      * the chosen action is inherently risky (registry approval_required), or
#      * the model is not confident enough, or
#      * the situation is severe enough that a human must look, or
#      * the model itself says "escalate".
# ==========================================================================
AUTONOMY_POLICY: dict = {
    "enabled": True,
    "min_confidence": 0.7,
    "escalate_severities": ["critical"],
    "allowed_actions": [
        "apply_label", "assign_issue", "draft_reply",
        "comment_on_pr", "page_engineer",
    ],
    # assignment is decided by the project manager clicking a link in the email
    "assignment_requires_human": True,
    "escalation_action": "page_engineer",  # what to queue for a human on escalate
    "max_actions_per_run": 5,
}


# ==========================================================================
# 8. Briefing policy — what the operator should do next. ORDER MATTERS: the
#    first rule whose conditions all hold wins, and the last rule (no `when`)
#    is the default. A blocked human decision outranks "nothing is wrong".
# ==========================================================================
BRIEFING_POLICY: list[dict] = [
    {
        "mode": "waiting_for_signal",
        "when": [{"field": "sources_connected", "op": "eq", "value": 0}],
        "headline": "Connect a source so the OS can start watching real work.",
        "next_best_action": "Add a GitHub, Slack, or Zendesk connection, then scan the workspace.",
    },
    {
        "mode": "waiting_for_sync",
        "when": [{"field": "events", "op": "eq", "value": 0}],
        "headline": "Connected, but nothing has been pulled in yet.",
        "next_best_action": "Hit Sync on your connection, then scan the workspace.",
    },
    {
        # must outrank triage/monitoring: a pending approval blocks the loop
        "mode": "needs_human",
        "when": [{"field": "pending_approvals", "op": "gt", "value": 0}],
        "headline": "{pending_approvals} action{pending_approvals_s} waiting for approval.",
        "next_best_action": "Open the command center and approve, reject, or inspect the prepared action.",
    },
    {
        "mode": "triage_now",
        "when": [{"field": "high_risk", "op": "gt", "value": 0}],
        "headline": "{high_risk} high-priority situation{high_risk_s} to triage.",
        "next_best_action": "Open Flags and review the prepared next action.",
    },
    {
        "mode": "triage_queue",
        "when": [{"field": "active_situations", "op": "gt", "value": 0}],
        "headline": "{active_situations} situation{active_situations_s} ready for triage.",
        "next_best_action": "Review the evidence chain and choose an action.",
    },
    {
        "mode": "monitoring",
        "when": [],  # default — must stay last
        "headline": "No active risks in the current signal set.",
        "next_best_action": "Keep monitoring, or scan again after the next sync.",
    },
]
