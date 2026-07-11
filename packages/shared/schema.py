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
    n: int
    median: float
    mean: float
    std: float
    window_days: int
    computed_at: datetime


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
    status: str  # pending_approval | executed | dry_run | rejected | failed
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


class TeamMember(BaseModel):
    """A person the system can notify or assign work to."""

    id: str  # the source login used to assign (e.g. GitHub username)
    name: str
    email: str
    roles: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    max_open_issues: int = 3
    assignable: bool = True
