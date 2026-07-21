from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Actor(BaseModel):
    """Who performed an event."""

    id: str
    name: str
    email: str | None = None


class Event(BaseModel):
    """Generic, source-agnostic event.

    The core engine stores and searches these. Verticals decide which
    ``source`` / ``type`` values are meaningful — the schema itself carries no
    domain logic.
    """

    id: str
    company_id: str = "default"
    source: str  # "github" | "slack" | "zendesk" | ...
    type: str  # e.g. "issue", "message", "ticket"
    actor: Actor
    timestamp: datetime
    content: str  # the text we embed + full-text index
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)  # untouched source payload
    backfilled: bool = False  # from history walk vs. a live sync/webhook

    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Understand layer — generic graph + resolution + norms result types.
# The core produces these; verticals supply the rules that drive them.
# ---------------------------------------------------------------------------


class ResolvedEntity(BaseModel):
    """One link discovered by core.resolve() between two events/nodes."""

    source_event_id: str
    target_node_id: str
    node_type: str
    method: str  # "id" | "embedding" | "keyword"
    edge_type: str  # "SAME_AS" | "MENTIONS" | "CLOSES" | "AUTHORED"
    confidence: float


class GraphNode(BaseModel):
    id: str
    company_id: str = "default"
    type: str  # Incident | PullRequest | Ticket | Feature | Person | Message
    key: str
    label: str
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    company_id: str = "default"
    src_id: str
    dst_id: str
    type: str  # AUTHORED | CLOSES | MENTIONS | SAME_AS | CAUSED_BY
    weight: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphResult(BaseModel):
    center: str
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


class NormBaseline(BaseModel):
    company_id: str
    metric: str
    unit: str
    n: int  # sample count BEFORE outlier trimming — what maturity gates on
    median: float  # of the outlier-trimmed observations
    mean: float  # trend-adjusted "right now" estimate, not a flat historical average
    std: float  # of the outlier-trimmed observations
    trend_per_period: float = 0.0  # slope: metric units per observation, oldest -> newest
    maturity: str = "insufficient"  # insufficient | learning | stable | unmeasurable, gated on n
    window_days: int
    computed_at: datetime
    scope: str = "business"  # "business" (customer rhythms) | "system" (self-monitoring)


# ---------------------------------------------------------------------------
# Alert layer — situations, briefs, delivery.
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    """One cited fact backing a situation."""

    event_id: str
    source: str
    timestamp: datetime
    excerpt: str
    url: str | None = None


class Choice(BaseModel):
    """One option on a clarification situation — a genuine fork, not a nudge.

    ``effect`` is a small closed vocabulary the core dispatches generically
    (like a move's `kind`): "reset_norm" | "disable_source" | "snooze" | "none".
    ``effect_args`` carries whatever that effect needs (e.g. {"metric": ...}).
    """

    id: str
    label: str
    effect: str = "none"
    effect_args: dict[str, Any] = Field(default_factory=dict)


class Situation(BaseModel):
    id: str
    company_id: str
    rule: str
    severity: str  # low | medium | high | critical
    title: str
    summary: str = ""
    recommended_action: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    status: str = "open"  # open | delivered | acknowledged | resolved
    created_at: datetime
    resolved_at: datetime | None = None
    kind: str = "business"  # business | system (hidden by default) | clarification
    choices: list[Choice] | None = None  # only set for kind="clarification"
    resolved_choice: str | None = None  # the Choice.id the user picked
    resolved_by: str | None = None
    snoozed_until: datetime | None = None  # a "remind me later" choice sets this


class ActionLink(BaseModel):
    """A one-click button rendered into a delivered brief (e.g. an email)."""

    label: str
    url: str
    primary: bool = False


class Brief(BaseModel):
    situation_id: str
    title: str
    severity: str
    summary: str
    recommended_action: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    assigned_to: str | None = None
    action_links: list[ActionLink] = Field(default_factory=list)
    note: str = ""  # extra context rendered above the buttons


class AssigneeDecision(BaseModel):
    """Who the agent picked to own the work, and how sure it is."""

    assignee: str  # a candidate id, or "none"
    confidence: float = 0.0
    rationale: str = ""


class DeliveryReceipt(BaseModel):
    situation_id: str
    channel: str
    recipient: str
    status: str
    delivered_at: datetime


# ---------------------------------------------------------------------------
# Act layer — actions with human-approval gating.
# ---------------------------------------------------------------------------


class ActionRequest(BaseModel):
    company_id: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    situation_id: str | None = None
    requested_by: str = "system"


class ActionDecision(BaseModel):
    """What the agent decided to do about a situation, and how sure it is."""

    action: str  # a registered action name, or "escalate"
    argument: str = ""  # opaque payload the vertical maps into action params
    confidence: float = 0.0  # 0..1
    rationale: str = ""


class ActionResult(BaseModel):
    id: int | None = None
    action: str
    # pending_approval | executed | recorded | dry_run | rejected | failed
    #   executed = a real external system was contacted
    #   recorded = a `log` move: intent captured, nothing left the building
    status: str
    detail: str = ""
    result: dict[str, Any] = Field(default_factory=dict)


class Connection(BaseModel):
    company_id: str
    source: str
    connected: bool
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class Ticket(BaseModel):
    """A concrete piece of work someone owns."""

    id: int | None = None
    company_id: str = "default"
    situation_id: str | None = None
    title: str
    description: str = ""
    assignee: str
    status: str = "open"  # open | done
    source_event_id: str | None = None
    external_url: str | None = None
    created_at: datetime | None = None
    closed_at: datetime | None = None


class Message(BaseModel):
    """One turn in a conversation. ``artifacts`` carries structured inline
    blocks — a clarification, an evidence block, an approval request — the
    same pattern regardless of which situation produced them."""

    id: int | None = None
    conversation_id: int
    role: str  # user | agent | system
    content: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime | None = None


class Conversation(BaseModel):
    id: int | None = None
    company_id: str
    title: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TeamMember(BaseModel):
    """A person the system can notify or assign work to."""

    id: str  # the source login used to assign (e.g. GitHub username)
    name: str
    email: str
    roles: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    max_open_issues: int = 3
    assignable: bool = True


# --------------------------------------------------------------------------
# Workflows — saved agentic plans (Phase 2).
# --------------------------------------------------------------------------


class WorkflowStep(BaseModel):
    """One step of a compiled plan. ``tool``/``args`` map onto a tool the agent
    already has, so a plan can never propose something the engine can't do.
    ``requires_approval`` is decided at plan time from the tool + the profile;
    it is advisory — the real brake is still act()'s gate at run time.

    ``select`` makes a run_action step DYNAMIC: instead of a fixed
    ``args.situation_id`` captured when the plan was written, the step targets
    every currently-open situation matching the selector, resolved fresh at run
    time. This is what lets a saved/scheduled workflow ("label all unassigned
    bugs each morning") act on today's items, not the ones that happened to be
    open the day it was planned. Selector keys are generic: ``rule`` (a
    watcher/profile rule name) and/or ``severity``."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    description: str = ""  # plain-language "what this step does", for the review UI
    requires_approval: bool = False
    select: dict[str, Any] | None = None  # run-action fan-out target, resolved at run time
    enabled: bool = True  # a disabled node is kept in the graph but skipped at run time


class WorkflowTrigger(BaseModel):
    """What starts a workflow — the n8n 'trigger node'. Three kinds:
      manual   — a human hits Run. config: {}
      schedule — a cron cadence.     config: {"cron": "0 9 * * *", "label": "every morning"}
      event    — a raised situation matches. config: {"rule"?: str, "severity"?: str}
    All three fire: manual from the UI, schedule from a once-a-minute cron scan,
    event from a detection pass (apps.common.triggers). Unattended runs take the
    same gated path a manual one does — dry-run brake plus allowlist."""

    type: str = "manual"  # manual | schedule | event
    config: dict[str, Any] = Field(default_factory=dict)


class Workflow(BaseModel):
    id: int | None = None
    company_id: str
    name: str
    goal: str  # the natural-language intent the plan was compiled from
    steps: list[WorkflowStep] = Field(default_factory=list)
    trigger: WorkflowTrigger = Field(default_factory=WorkflowTrigger)
    enabled: bool = True
    created_by: str = "ui"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_run_at: datetime | None = None


class WorkflowStepResult(BaseModel):
    """What actually happened when a step ran — the honest receipt, distinct
    from the plan's intent. ``status`` mirrors act()'s vocabulary for action
    steps (executed | dry_run | pending_approval | recorded | failed) and is
    "done"/"failed" for read/control steps."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    status: str
    detail: str = ""
    result: dict[str, Any] = Field(default_factory=dict)


class WorkflowRun(BaseModel):
    id: int | None = None
    workflow_id: int
    company_id: str
    status: str = "running"  # planning | running | needs_approval | done | failed
    trigger: str = "manual"  # what caused THIS run: manual | scheduled | event
    step_results: list[WorkflowStepResult] = Field(default_factory=list)
    summary: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None


class WorkflowPlan(BaseModel):
    """The planner's output before anything is saved or run: the proposed
    trigger + steps plus any questions that must be answered first. A plan with
    open ``clarifications`` is not runnable — it asks at creation time, when the
    person has the context, rather than guessing (the question-budget rule)."""

    goal: str
    name: str  # a short suggested name for the workflow
    trigger: WorkflowTrigger = Field(default_factory=WorkflowTrigger)
    steps: list[WorkflowStep] = Field(default_factory=list)
    clarifications: list[str] = Field(default_factory=list)
