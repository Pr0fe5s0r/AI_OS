from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.connectors.base import (
    activity_field_for,
    moves_for,
    reviewers_for,
    target_spec_for,
)
from packages.core.ingest import apply_declared_mapping
from packages.core.norms import reset_norms

# The domain profile: pure DATA, one row per company. Seven slots. This module
# is the ONLY way domain knowledge enters the engine — loaded from the profiles
# table and passed into core functions as arguments. No code imports a vertical.

SLOTS = (
    "sources", "things", "links", "rhythms", "watchers", "moves", "vocabulary",
    # A reviewer is a grounded specialist fan-out over a Thing's content — the
    # blog's PR reviewer, generalized. It lives in the profile row like every
    # other slot (never a YAML file); a connector supplies sensible defaults
    # the same read-time way it supplies moves (see with_connector_reviewers).
    "reviewers",
)


class Profile(BaseModel):
    company_id: str
    version: int = 1
    status: str = "confirmed"  # proposed | confirmed
    sources: list[dict[str, Any]] = Field(default_factory=list)
    things: dict[str, Any] = Field(default_factory=dict)
    links: dict[str, Any] = Field(default_factory=dict)
    rhythms: list[dict[str, Any]] = Field(default_factory=list)
    watchers: list[dict[str, Any]] = Field(default_factory=list)
    moves: dict[str, Any] = Field(default_factory=dict)
    vocabulary: dict[str, Any] = Field(default_factory=dict)
    reviewers: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def slots(self) -> dict[str, Any]:
        return {slot: getattr(self, slot) for slot in SLOTS}


def _from_row(company_id: str, version: int, status: str, slots: dict) -> Profile:
    return Profile(
        company_id=company_id,
        version=version,
        status=status,
        **{slot: slots.get(slot) or Profile.model_fields[slot].default_factory() for slot in SLOTS},  # type: ignore[misc,call-arg]
    )


async def load_profile(session: AsyncSession, company_id: str) -> Profile | None:
    """The active profile: highest confirmed version for this company."""
    row = (
        await session.execute(
            text(
                """
                SELECT version, status, slots FROM profiles
                WHERE company_id = :c AND status = 'confirmed'
                ORDER BY version DESC LIMIT 1
                """
            ),
            {"c": company_id},
        )
    ).first()
    if row is None:
        return None
    slots = row.slots if isinstance(row.slots, dict) else json.loads(row.slots)
    profile = _from_row(company_id, row.version, row.status, slots)
    return with_connector_reviewers(with_connector_moves(with_connector_mapping(profile)))


def with_connector_mapping(profile: Profile) -> Profile:
    """Fold each connected source's DECLARED mapping fields into its induced
    mapping, on every read.

    Discovery induces a mapping once and freezes it into a profile version.
    That is right for what it learned from the payloads — but not for what the
    connector simply states, the way it states its moves. When a connector
    starts reporting something new (which files a pull request touched), a
    workspace discovered last week would otherwise stay blind to it forever,
    with nothing on screen to suggest anything was missing and no reason for
    anyone to re-run discovery.

    Same rule as with_connector_moves, for the same reason: induced knowledge
    is versioned and auditable; declared knowledge tracks the code that
    declares it. Nothing is written back — see save_profile.
    """
    sources = []
    kinds: list[str] = []
    for source in profile.sources:
        mapping = source.get("mapping")
        if source.get("kind") != "connector" or not isinstance(mapping, dict):
            sources.append(source)
            continue
        kind = str(source.get("source") or "")
        kinds.append(kind)
        sources.append({**source, "mapping": apply_declared_mapping(kind, mapping)})

    # Which field says "this last moved" is the same sort of fact: the
    # connector knows it, and every stall watcher depends on it. Only filled
    # when the profile has not already decided — a value induced by discovery,
    # or edited by a person, always wins.
    things = profile.things
    if not (things or {}).get("activity_field"):
        for kind in kinds:
            if declared := activity_field_for(kind):
                things = {**(things or {}), "activity_field": declared}
                break
    return profile.model_copy(update={"sources": sources, "things": things})


def with_connector_moves(profile: Profile) -> Profile:
    """Fill the action registry from what this company's CONNECTED SOURCES say
    they can do, rather than from anything a person had to author.

    Capability is transport knowledge — only code that speaks the GitHub API
    knows the call for applying a label — so each connector declares it and the
    engine asks. Everything situational (may this run unattended, which label,
    which assignee) is decided by what the engine has learned and by what the
    operator has told the agent, and is NOT overwritten here: a move already
    present in the stored profile wins, so a policy decision made in the
    product is never clobbered by a redeploy of the connector.

    Applied on every load, so connecting a source immediately grants its
    actions and disconnecting one takes them away — no reseed, no file.
    """
    registry: dict[str, Any] = {}
    targets: dict[str, dict[str, Any]] = {}
    for source in profile.sources:
        if source.get("enabled") is False:
            continue
        kind = str(source.get("source") or "")
        registry.update(moves_for(kind))
        # targeting is PER SOURCE: a Zendesk ticket URL and a GitHub issue URL
        # have nothing in common, so one global pattern could only ever serve
        # whichever connector happened to be first.
        pattern, params = target_spec_for(kind)
        if pattern:
            targets[kind] = {"pattern": pattern, "params": params}

    if not registry:
        return profile

    moves = dict(profile.moves)
    stored = moves.get("registry") or {}
    # Merge FIELD BY FIELD, not entry by entry. Whole-entry replacement meant a
    # stored move shadowed the declared one completely, so a decision as small
    # as approval_required froze that action at the shape it had when it was
    # stored — it never saw a corrected URL or a newly declared argument
    # vocabulary. Per-field: capability refreshes, decisions persist.
    moves["registry"] = {
        name: {**registry.get(name, {}), **stored.get(name, {})}
        for name in {*registry, *stored}
    }
    moves["targets"] = {**targets, **(moves.get("targets") or {})}
    return profile.model_copy(update={"moves": moves})


def _declared_reviewers(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Every reviewer this company's ENABLED connected sources declare, by key."""
    declared: dict[str, Any] = {}
    for source in sources:
        if source.get("enabled") is False:
            continue
        for reviewer in reviewers_for(str(source.get("source") or "")):
            if key := reviewer.get("key"):
                declared[key] = reviewer
    return declared


def with_connector_reviewers(profile: Profile) -> Profile:
    """Fill the reviewers slot from what this company's CONNECTED SOURCES
    declare, exactly as with_connector_moves fills the action registry.

    A reviewer is transport-shaped knowledge — only the GitHub connector knows
    a pull request carries a diff, a commit list and a review thread worth
    reading — so the connector declares it and the engine asks. Merged by key:
    an entry a person stored in the profile row WINS whole (a deliberate policy
    override survives a connector redeploy), everything else comes from the
    declared default. Applied on every load, so connecting a source grants its
    reviewer and disconnecting one removes it — no reseed, no file.
    """
    declared = _declared_reviewers(profile.sources)
    if not declared:
        return profile
    stored = {r["key"]: r for r in profile.reviewers if r.get("key")}
    merged = {**declared, **stored}  # stored override wins by key
    # keep declaration order, with any purely-stored extras appended
    ordered = [merged[k] for k in declared] + [
        merged[k] for k in stored if k not in declared
    ]
    return profile.model_copy(update={"reviewers": ordered})


def without_connector_reviewers(profile: Profile) -> Profile:
    """Inverse of with_connector_reviewers — strip entries still IDENTICAL to
    what a connector declares, so capability is never frozen into a stored row
    (the same reason without_connector_moves exists). The moment someone edits
    a reviewer it stops matching the declaration and is kept as a decision.
    """
    declared = _declared_reviewers(profile.sources)
    kept = [r for r in profile.reviewers if declared.get(str(r.get("key") or "")) != r]
    return profile.model_copy(update={"reviewers": kept})


def _declared_moves(sources: list[dict[str, Any]]) -> dict[str, Any]:
    declared: dict[str, Any] = {}
    for source in sources:
        declared.update(moves_for(str(source.get("source") or "")))
    return declared


def without_connector_moves(profile: Profile) -> Profile:
    """Inverse of :func:`with_connector_moves` — strip everything a connector
    declared, leaving only what a person or the engine actually decided.

    Applied before every write, because capability is a READ-TIME overlay. If
    it were persisted instead, a stored row would freeze whatever the connector
    happened to support on the day it was saved, and disconnecting a source
    would no longer take its actions away. Only entries still IDENTICAL to the
    declared spec are removed: the moment someone edits one it stops being
    capability and becomes a decision worth keeping.
    """
    moves = dict(profile.moves)
    registry = dict(moves.get("registry") or {})
    for name, declared in _declared_moves(profile.sources).items():
        entry = registry.get(name)
        if not isinstance(entry, dict):
            continue
        # keep only the fields that DIFFER from what the connector declares —
        # the decision, not the capability it was layered on top of
        delta = {k: v for k, v in entry.items() if declared.get(k) != v}
        if delta:
            registry[name] = delta
        else:
            registry.pop(name, None)
    moves["registry"] = registry
    moves.pop("targets", None)  # always derived, never stored
    return profile.model_copy(update={"moves": moves})


def _rhythm_key(rhythm: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (rhythm.get("source"), rhythm.get("type"), rhythm.get("end_field"))


def _rhythms_needing_reset(
    old_rhythms: list[dict[str, Any]], new_rhythms: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rhythms that changed what "finished" means, between two confirmed
    versions. A brand-new rhythm (name didn't exist before) has no prior
    history to protect against and is excluded."""
    old_by_name = {r["name"]: _rhythm_key(r) for r in old_rhythms if r.get("name")}
    return [
        r
        for r in new_rhythms
        if r.get("name") in old_by_name and _rhythm_key(r) != old_by_name[r["name"]]
    ]


async def save_profile(
    session: AsyncSession, profile: Profile, status: str = "confirmed"
) -> int:
    """Insert a new profile version (next version number). Returns the version.

    Confirming a version whose rhythms changed what "finished" means (a
    different end_field, or a different source/type) resets that metric's
    baseline automatically, dated to right now. Without this, history measured
    under the OLD meaning silently blends with the new one, and the drift
    detector would flag the change itself as "your times changed!" when the
    real story is "we changed the question."

    Two confirms for the same company overlapping in time is a real hazard:
    the version number is a "read the max, then insert" computation, and
    under READ COMMITTED a concurrent transaction's read doesn't see the
    other's uncommitted insert — both could compute the same next_version, or
    diff against the same now-stale `previous` and each fire its own reset.
    A Postgres advisory lock scoped to this transaction serializes confirms
    per company (never across companies), so the second one waits and then
    reads the FIRST one's committed result as its `previous` — no lost
    writes, no split-brain resets, and it releases itself automatically at
    commit or rollback, so nothing can leak it on an error path.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:c)::bigint)"), {"c": profile.company_id}
    )
    # capability is derived on load; storing it would freeze it (see
    # without_connector_moves) and make every boot look like a change
    profile = without_connector_reviewers(without_connector_moves(profile))

    if status == "confirmed":
        previous = await load_profile(session, profile.company_id)
        if previous is not None:
            reset_at = datetime.now(UTC)
            for rhythm in _rhythms_needing_reset(previous.rhythms, profile.rhythms):
                await reset_norms(
                    session, profile.company_id, rhythm["name"], reset_at,
                    reset_by="profile_change", defn=rhythm,
                )

    next_version = (
        await session.execute(
            text("SELECT coalesce(max(version), 0) + 1 FROM profiles WHERE company_id = :c"),
            {"c": profile.company_id},
        )
    ).scalar_one()
    await session.execute(
        text(
            """
            INSERT INTO profiles (company_id, version, status, slots)
            VALUES (:c, :v, :s, CAST(:slots AS jsonb))
            """
        ),
        {
            "c": profile.company_id,
            "v": next_version,
            "s": status,
            "slots": json.dumps(profile.slots),
        },
    )
    return int(next_version)


async def set_source_enabled(
    session: AsyncSession, company_id: str, source: str, enabled: bool
) -> Profile | None:
    """Turn watching a source off (or back on) as a NEW confirmed version.

    This is a genuine business-fact change (the customer told us to stop — or
    resume — watching a tool), so it goes through the same versioning scheme a
    seed reload uses: the prior version is kept, never overwritten — an audit
    trail of how the business's profile changed over time.

    Returns None if the company has no profile or no matching source.
    """
    current = await load_profile(session, company_id)
    if current is None or not any(s.get("source") == source for s in current.sources):
        return None
    sources = [
        {**s, "enabled": enabled} if s.get("source") == source else s
        for s in current.sources
    ]
    updated = current.model_copy(update={"sources": sources})
    version = await save_profile(session, updated, status="confirmed")
    updated.version = version
    return updated


async def disable_source(session: AsyncSession, company_id: str, source: str) -> Profile | None:
    """Stop watching a source. The clarification effect `disable_source` calls this."""
    return await set_source_enabled(session, company_id, source, enabled=False)


async def enable_source(session: AsyncSession, company_id: str, source: str) -> Profile | None:
    """Resume watching a source that was turned off.

    Without this, `disable_source` is a one-way door: the "stop watching"
    choice on a clarification card could never be undone from inside the
    product, and its sibling choice ("help me reconnect") would have nothing
    to reconnect with.
    """
    return await set_source_enabled(session, company_id, source, enabled=True)


async def set_action_approval(
    session: AsyncSession, company_id: str, action: str, required: bool
) -> Profile | None:
    """Change whether one move waits for a human, as a NEW confirmed version.

    This is how "stop asking me before labelling things" becomes a real
    policy change rather than a preference buried in someone's head — and
    because every version is kept, you can always see when the bar moved and
    who moved it. Returns None if the move isn't registered.
    """
    current = await load_profile(session, company_id)
    if current is None:
        return None
    registry = dict((current.moves or {}).get("registry") or {})
    if action not in registry:
        return None
    registry[action] = {**registry[action], "approval_required": bool(required)}
    moves = {**current.moves, "registry": registry}
    updated = current.model_copy(update={"moves": moves})
    updated.version = await save_profile(session, updated, status="confirmed")
    return updated


async def set_autonomy(
    session: AsyncSession, company_id: str, **changes: Any
) -> Profile | None:
    """Tune how boldly the AI acts — which moves it may take alone, how sure
    it must be, which severities always reach a human. Unknown keys are
    ignored so a model cannot invent policy fields."""
    allowed_keys = {
        "enabled", "min_confidence", "escalate_severities",
        "allowed_actions", "assignment_requires_human", "max_actions_per_run",
        # "post to a customer-visible thread without asking me" — off unless
        # someone says otherwise; see core.act.needs_approval
        "allow_public_actions",
    }
    patch = {k: v for k, v in changes.items() if k in allowed_keys and v is not None}
    if not patch:
        return None
    current = await load_profile(session, company_id)
    if current is None:
        return None
    autonomy = {**((current.moves or {}).get("autonomy") or {}), **patch}
    moves = {**current.moves, "autonomy": autonomy}
    updated = current.model_copy(update={"moves": moves})
    updated.version = await save_profile(session, updated, status="confirmed")
    return updated


async def ensure_company(session: AsyncSession, company_id: str, name: str) -> None:
    """Create the company row if it's missing. Deliberately does NOT rename an
    existing one: callers that only have an id pass the id as the name, and
    overwriting on conflict renamed real workspaces to their slug the first
    time discovery ran. A workspace is named by the person who created it."""
    await session.execute(
        text(
            """
            INSERT INTO companies (id, name) VALUES (:id, :name)
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"id": company_id, "name": name},
    )
