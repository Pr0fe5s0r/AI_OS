from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime
from typing import Any

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.norms import reset_norms

# The domain profile: pure DATA, one row per company. Seven slots. This module
# is the ONLY way domain knowledge enters the engine — loaded from the profiles
# table and passed into core functions as arguments. No code imports a vertical.

SLOTS = ("sources", "things", "links", "rhythms", "watchers", "moves", "vocabulary")


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
    return _from_row(company_id, row.version, row.status, slots)


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
    await session.execute(
        text(
            """
            INSERT INTO companies (id, name) VALUES (:id, :name)
            ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
            """
        ),
        {"id": company_id, "name": name},
    )


def read_profile_yaml(path: str | pathlib.Path) -> Profile:
    """Parse a seed file (profiles/*.yaml) into a Profile."""
    data = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
    company_id = data["company_id"]
    return Profile(
        company_id=company_id,
        **{slot: data.get(slot) or Profile.model_fields[slot].default_factory() for slot in SLOTS},  # type: ignore[misc,call-arg]
    )


def _comparable(slots: dict[str, Any]) -> dict[str, Any]:
    """``slots``, with runtime-only toggles (like a clarification's
    disable_source) stripped, for reseed-idempotency comparison only.

    Without this, every boot's seed_all_profiles() would see the DB's
    `enabled: false` as a "diff" from the untouched YAML and silently write
    a fresh version reverting the user's disable — a redeploy would undo a
    business decision made through the app. The DB stays the source of truth
    for what actually loads; only the equality check ignores this field.
    """
    sources = [{k: v for k, v in s.items() if k != "enabled"} for s in slots.get("sources", [])]
    return {**slots, "sources": sources}


async def seed_profile(
    session: AsyncSession, path: str | pathlib.Path, name: str | None = None
) -> Profile:
    """Load a YAML seed into the profiles table (idempotent by content).

    If the latest confirmed profile already has identical slots, nothing is
    written — reseeding is safe.
    """
    profile = read_profile_yaml(path)
    await ensure_company(session, profile.company_id, name or profile.company_id)
    current = await load_profile(session, profile.company_id)
    if current is not None and _comparable(current.slots) == _comparable(profile.slots):
        return current
    version = await save_profile(session, profile, status="confirmed")
    profile.version = version
    return profile
