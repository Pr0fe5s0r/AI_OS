from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.credentials import get_credential
from packages.core.profile import Profile, load_profile
from packages.core.settings import get_setting

DRY_RUN_KEY = "dry_run"
TEAM_KEY = "team_roster"


class ProfileNotFound(Exception):
    pass


async def get_profile(session: AsyncSession, company_id: str) -> Profile:
    profile = await load_profile(session, company_id)
    if profile is None:
        raise ProfileNotFound(
            f"no confirmed profile for company {company_id!r} — connect a source and "
            "let discovery propose one"
        )
    return profile


# ------------------------- source configs from a profile -------------------------


def _context_value(config: dict, spec: dict) -> str:
    value = str(config.get(spec.get("from_config", ""), ""))
    if spec.get("op") == "basename":
        value = value.split("/")[-1]
    return value


def build_source_config(profile: Profile, source_def: dict, config: dict) -> dict:
    """The declarative normalizer input for core.ingest()."""
    context = {
        key: _context_value(config, spec)
        for key, spec in (source_def.get("context") or {}).items()
    }
    return {
        "source": source_def["source"],
        "company_id": profile.company_id,
        "context": context,
        "mapping": source_def["mapping"],
        # connector_type drives raw-payload schema validation in the pipeline;
        # None for `kind: push` sources, which declare no connector-side schema.
        "connector_type": source_def.get("connector"),
    }


def resolve_cfg(profile: Profile) -> dict:
    """What the pipeline's resolve job needs: the things + links slots."""
    return {"things": profile.things, "links": profile.links}


async def connector_specs(
    session: AsyncSession, profile: Profile
) -> list[dict]:
    """Connector transport specs for every connected `kind: connector` source.

    Credentials come from the sealed store; GITHUB_REPO env remains as the
    headless-dev fallback for a source whose connector is 'github'.
    """
    specs: list[dict] = []
    for source_def in profile.sources:
        if source_def.get("kind") != "connector" or not source_def.get("enabled", True):
            continue
        connector = source_def["connector"]
        cred = await get_credential(session, profile.company_id, source_def["source"])
        token, config = cred if cred else ("", {})

        required = [f["key"] for f in source_def.get("config_fields", [])]
        if not all(config.get(k) for k in required):
            # headless-dev fallback: the profile may map config keys to env vars
            env_fallback = source_def.get("env_fallback") or {}
            env_config = {
                key: os.environ[var]
                for key, var in env_fallback.items()
                if key != "token" and os.getenv(var)
            }
            if not all(env_config.get(k) for k in required):
                continue
            token = token or os.getenv(env_fallback.get("token", ""), "")
            config = env_config

        specs.append(
            {
                "type": connector,
                "source": source_def["source"],
                "token": token or None,
                "limit": int(config.get("limit", 30)),
                **{k: v for k, v in config.items() if k not in ("limit",)},
                "source_config": build_source_config(profile, source_def, config),
            }
        )
    return specs


def push_source(profile: Profile, source: str) -> dict | None:
    """The profile's `kind: push` source definition, if it exists and is enabled."""
    for source_def in profile.sources:
        if (
            source_def.get("kind") == "push"
            and source_def["source"] == source
            and source_def.get("enabled", True)
        ):
            return source_def
    return None


# ------------------------------ policy + team ------------------------------


async def approval_policy(session: AsyncSession, profile: Profile) -> dict:
    """The approval policy in force right now, honoring the operator's toggle."""
    defaults = profile.moves.get("approval_defaults", {}) or {}
    dry_run = await get_setting(
        session, profile.company_id, DRY_RUN_KEY, defaults.get("dry_run", True)
    )
    # "may an action that posts where other people read it run unattended?" is
    # the same KIND of decision as the other autonomy knobs (how sure it must
    # be, which severities always escalate), so it is read from there rather
    # than becoming a second place to look. The gate that consumes it is
    # core.act.needs_approval; absent means no, which is the safe direction.
    autonomy = profile.moves.get("autonomy", {}) or {}
    return {
        **defaults,
        "dry_run": bool(dry_run),
        "allow_public_actions": bool(autonomy.get("allow_public_actions", False)),
    }


async def team_roster(session: AsyncSession, company_id: str) -> list[dict]:
    roster = await get_setting(session, company_id, TEAM_KEY, None)
    return list(roster) if roster else []


async def save_team_roster(
    session: AsyncSession, company_id: str, roster: list[dict]
) -> None:
    from packages.core.settings import set_setting

    await set_setting(session, company_id, TEAM_KEY, roster)


def recipients_for(roles: list[str], roster: list[dict]) -> list[str]:
    return [
        m["email"]
        for m in roster
        if m.get("email") and any(r in m.get("roles", []) for r in roles)
    ]


def routing_config(profile: Profile, dry_run: bool, roster: list[dict]) -> dict:
    """Every raised situation notifies the profile's notify roles."""
    team = profile.moves.get("team", {}) or {}
    to = recipients_for(team.get("notify_roles", []), roster)
    email_route = {"channel": "email", "recipients": to}
    return {
        "from": os.getenv("SMTP_FROM", "ai-os@localhost"),
        "dry_run": dry_run,
        "routes": {sev: email_route for sev in ("critical", "high", "medium", "low")},
        "default": {"channel": "console", "recipients": to},
    }
