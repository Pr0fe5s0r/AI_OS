from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apps.common.context import connector_specs
from packages.connectors.base import SUPPORTED_SOURCES, build_connector
from packages.core.credentials import get_credential
from packages.core.discovery import induce_profile
from packages.core.profile import Profile, ensure_company, load_profile, save_profile

# Onboarding a company (checkpoint 5): connect a source, look at what it
# really returns, propose a profile, let a human confirm it.
#
# The chicken-and-egg this solves: ingestion needs a profile (to normalize raw
# payloads into Events), so a brand-new company can't ingest anything first and
# learn from its own events. Discovery therefore reads RAW payloads straight
# off the connector — the one thing available before any profile exists.
#
# The prompt lives here, not in core: core.discovery takes it as an argument,
# the same way detect() and agent take theirs. A company being onboarded has no
# profile yet, so there is no profile.vocabulary to read it from.

INDUCE_PROMPT = """You are setting up an operations system for a new company.
Below is a field inventory automatically extracted from real payloads pulled
from one of their tools, plus a couple of raw samples.

{inventory}

Raw samples:
{samples}

Name and map this data so the system can watch it:
- thing_type: PascalCase name for what one record IS to this business (e.g. Incident, Ticket, PurchaseOrder).
- event_type: snake_case name for the event (e.g. issue, ticket, purchase_order).
- id_template: a globally unique id, using {{field}} placeholders that refer to TOP-LEVEL fields of the raw payload only. Prefer a stable numeric identifier.
- timestamp_field: the dotted path of the field marking when the record was created.
- title_field / body_field: the dotted paths carrying the human-readable summary and detail.
- status_field: the dotted path of the field holding the record's state, if any.
- actor_field: the dotted path to the STRING naming the person responsible (e.g. user.login).
- url_field: the dotted path to a link to the record, if any.
- end_field: the dotted path of the field that is only set once the record is FINISHED (used to measure how long work takes).
- metadata_fields: dotted paths worth keeping for filtering and detection.
- rhythm_name: snake_case name for the duration metric, e.g. issue_resolution_hours.
- vocabulary: the words this business would use for thing / situation / actor / workspace.

Use ONLY field paths that appear in the inventory above. Do not invent fields."""


async def _spec_from_credential(session: AsyncSession, company_id: str, source: str) -> dict | None:
    """A transport spec built from the stored connection ALONE — no profile.

    This is the real onboarding path: a new company connects a tool, and at
    that moment a credential + config exist but a profile does not. Requiring
    a profile here would be the exact chicken-and-egg discovery exists to
    break. Only works for a source whose name is a supported connector, which
    is precisely what the connect endpoint writes.
    """
    if source not in SUPPORTED_SOURCES:
        return None
    cred = await get_credential(session, company_id, source)
    if cred is None:
        return None
    token, config = cred
    return {
        "type": source,
        "source": source,
        "token": token or None,
        "limit": int(config.get("limit", 30)),
        **{k: v for k, v in config.items() if k != "limit"},
    }


async def _connector_spec(
    session: AsyncSession, company_id: str, source: str, template_company_id: str | None
) -> dict:
    """Find a way to read real payloads for ``source``, in the order that
    matches how a company actually arrives:

    1. this company's own connection (a new company that just connected) —
       no profile required;
    2. this company's profile, if it already has one;
    3. a template company's connection, for inducing a profile for someone
       who hasn't connected anything yet.
    """
    own = await _spec_from_credential(session, company_id, source)
    if own is not None:
        return own

    profile = await load_profile(session, company_id)
    if profile is not None:
        specs = await connector_specs(session, profile)
        spec = next((s for s in specs if s["source"] == source), None)
        if spec is not None:
            return spec

    if template_company_id:
        borrowed = await _spec_from_credential(session, template_company_id, source)
        if borrowed is not None:
            return borrowed
        template_profile = await load_profile(session, template_company_id)
        if template_profile is not None:
            specs = await connector_specs(session, template_profile)
            spec = next((s for s in specs if s["source"] == source), None)
            if spec is not None:
                return spec

    raise ValueError(
        f"{source!r} is not connected for {company_id!r} — connect it first "
        "(or pass template_company_id of a company that has it connected)"
    )


def _merge_new_source(base: Profile, induced: Profile, source: str) -> Profile:
    """Fold a newly-discovered source's mapping into the existing profile. Keep
    everything the workspace already learned (other sources, things, links,
    moves, vocabulary, reviewers); replace only this source's own entry and add
    any rhythms/watchers it proposed that aren't already there. `moves` and
    `reviewers` come back on load through the connector overlays once the source
    is present, so they need no merging here."""
    new_src = next((s for s in induced.sources if s.get("source") == source), None)
    sources = [s for s in base.sources if s.get("source") != source]
    if new_src is not None:
        sources.append(new_src)

    def _add(existing: list, extra: list) -> list:
        seen = {x.get("name") for x in existing if isinstance(x, dict)}
        return existing + [x for x in extra if isinstance(x, dict) and x.get("name") not in seen]

    return base.model_copy(update={
        "sources": sources,
        "rhythms": _add(base.rhythms, induced.rhythms),
        "watchers": _add(base.watchers, induced.watchers),
    })


async def propose_from_connector(
    session: AsyncSession,
    company_id: str,
    source: str,
    template_company_id: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    """Pull real payloads for ``source`` and save an induced profile as a
    PROPOSED version. Nothing is activated: the engine keeps running on the
    confirmed profile (if any) until a human confirms this one.

    ``template_company_id`` says whose connection to read the payloads through
    — for a brand-new company that has no profile of its own yet, point it at
    a company that already has this source connected.
    """
    spec = await _connector_spec(session, company_id, source, template_company_id)

    connector = build_connector({**spec, "limit": limit})
    raws = await connector.fetch_raw()
    if not raws:
        raise ValueError(f"{source!r} returned no payloads — nothing to learn from")

    profile, report = induce_profile(
        company_id, source, spec["type"], raws, INDUCE_PROMPT
    )
    # Adding a source must ADD to the profile, not replace it. induce_profile
    # only sees the new source's payloads, so on its own it proposes a profile
    # with just that source — confirming which would wipe every OTHER source the
    # workspace already learned (this silently deleted a GitHub setup the first
    # time a second tool was connected). Fold the new source into the existing
    # confirmed profile instead, keeping its things, moves, reviewers and other
    # sources intact.
    existing = await load_profile(session, company_id)
    if existing is not None:
        profile = _merge_new_source(existing, profile, source)
    await ensure_company(session, company_id, company_id)
    version = await save_profile(session, profile, status="proposed")
    profile.version = version

    return {
        "company_id": company_id,
        "source": source,
        "version": version,
        "status": "proposed",
        "valid": report["valid"],
        "errors": report["errors"],
        "used_llm": report["used_llm"],
        "payloads_examined": report["inventory"]["payloads"],
        "proposal": report["proposal"],
        "profile": profile.model_dump(mode="json"),
    }


async def confirm_proposed(session: AsyncSession, company_id: str, version: int) -> Profile:
    """Promote a proposed profile to confirmed — the human's yes.

    Saved as a NEW confirmed version rather than flipping the proposed row's
    status, keeping the same never-overwrite audit trail every other profile
    change uses.
    """
    row = await _load_version(session, company_id, version)
    if row is None:
        raise ValueError(f"no profile version {version} for {company_id!r}")
    if row.status != "proposed":
        raise ValueError(f"version {version} is already {row.status}")

    proposed = _profile_from_slots(company_id, row.slots)
    confirmed_version = await save_profile(session, proposed, status="confirmed")
    proposed.version = confirmed_version
    proposed.status = "confirmed"
    return proposed


async def _load_version(session: AsyncSession, company_id: str, version: int):
    from sqlalchemy import text

    return (
        await session.execute(
            text("SELECT version, status, slots FROM profiles WHERE company_id = :c AND version = :v"),
            {"c": company_id, "v": version},
        )
    ).first()


def _profile_from_slots(company_id: str, slots: Any) -> Profile:
    import json as _json

    from packages.core.profile import SLOTS

    data = slots if isinstance(slots, dict) else _json.loads(slots)
    return Profile(
        company_id=company_id,
        **{slot: data.get(slot) or Profile.model_fields[slot].default_factory() for slot in SLOTS},  # type: ignore[misc,call-arg]
    )


async def list_versions(session: AsyncSession, company_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import text

    rows = await session.execute(
        text(
            """
            SELECT version, status, created_at FROM profiles
            WHERE company_id = :c ORDER BY version DESC
            """
        ),
        {"c": company_id},
    )
    return [
        {"version": r.version, "status": r.status, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


__all__ = ["INDUCE_PROMPT", "confirm_proposed", "list_versions", "propose_from_connector"]
