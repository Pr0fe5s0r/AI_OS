from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import text

from apps.common.analysis import default_argument, extract_target, learned_arguments
from packages.core.db import Session
from packages.core.profile import (
    Profile,
    load_profile,
    save_profile,
    set_source_enabled,
    with_connector_moves,
    without_connector_moves,
)
from packages.shared.schema import Evidence, Situation

# Capability comes from CONNECTORS, not from a file a person had to author.
#
# The rule the whole design rests on: capability is a READ-TIME overlay. What a
# connector can do is derived on every load; what a person or the engine DECIDED
# is what gets stored. Confusing the two is how you end up freezing a connector's
# day-one abilities into a database row, or writing a new profile version on
# every single boot.

_CO = "test-capability"


def _profile(**kw) -> Profile:
    base = dict(
        company_id=_CO,
        sources=[{"source": "github", "type": "issue"}],
        things={}, links={}, rhythms=[], watchers=[], moves={}, vocabulary={},
    )
    base.update(kw)
    return Profile(**base)


def _situation() -> Situation:
    return Situation(
        id="s1", company_id=_CO, rule="unassigned_bug", severity="high",
        title="t", summary="s", recommended_action="do it",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        evidence=[Evidence(event_id="e1", source="github", timestamp="2026-01-01T00:00:00Z",
                           excerpt="x", url="https://github.com/a/b/issues/1")],
    )


async def _reset(session) -> None:
    await session.execute(text("DELETE FROM profiles WHERE company_id = :c"), {"c": _CO})
    await session.execute(
        text("INSERT INTO companies (id, name) VALUES (:c, :c) ON CONFLICT DO NOTHING"),
        {"c": _CO},
    )
    await session.commit()


# ------------------------------ the overlay ------------------------------


def test_connecting_github_grants_its_actions_with_no_file() -> None:
    effective = with_connector_moves(_profile())
    registry = effective.moves["registry"]
    # every action is one GitHub really supports, declared beside its API code
    assert {"apply_label", "assign_issue", "comment_on_issue", "close_issue", "create_issue"} <= set(registry)
    assert registry["apply_label"]["kind"] == "http"


def test_a_source_with_no_writes_grants_nothing() -> None:
    """Slack and Zendesk are read-only. An empty registry is the honest state
    for a connector that cannot act — not something to fill with stubs."""
    effective = with_connector_moves(_profile(sources=[{"source": "slack", "type": "message"}]))
    assert effective.moves.get("registry", {}) == {}


def test_disabling_a_source_takes_its_actions_away() -> None:
    effective = with_connector_moves(
        _profile(sources=[{"source": "github", "type": "issue", "enabled": False}])
    )
    assert effective.moves.get("registry", {}) == {}


def test_capability_carries_no_policy() -> None:
    """A connector says what CAN be done, never whether it may run unattended
    or with what argument — those are learned or decided, not transport facts."""
    registry = with_connector_moves(_profile()).moves["registry"]
    for name, spec in registry.items():
        assert "approval_required" not in spec, name
        assert "default_argument" not in spec, name


# --------------------------- stored vs derived ---------------------------


async def test_derived_capability_is_never_written_to_the_row() -> None:
    async with Session() as session:
        await _reset(session)
        await save_profile(session, with_connector_moves(_profile()))
        await session.commit()
        stored = (
            await session.execute(
                text("SELECT slots FROM profiles WHERE company_id = :c ORDER BY version DESC LIMIT 1"),
                {"c": _CO},
            )
        ).scalar_one()

    registry = (stored.get("moves") or {}).get("registry") or {}
    assert registry == {}, "capability is derived on load; storing it would freeze it"


async def test_a_decision_survives_and_beats_the_declared_spec() -> None:
    """Editing a move turns it from capability into a decision, and a decision
    must never be clobbered by a connector redeploy."""
    async with Session() as session:
        await _reset(session)
        edited = with_connector_moves(_profile())
        edited.moves["registry"]["apply_label"] = {
            **edited.moves["registry"]["apply_label"], "approval_required": True,
        }
        await save_profile(session, edited)
        await session.commit()
        loaded = await load_profile(session, _CO)

    assert loaded is not None
    assert loaded.moves["registry"]["apply_label"]["approval_required"] is True
    # and it really is in the row, not re-derived
    assert "apply_label" in without_connector_moves(loaded).moves["registry"]


async def test_repeated_saves_do_not_inflate_the_version() -> None:
    """The regression this design caused once: load enriches, save stores the
    enrichment, next comparison sees a diff — a new version on every boot."""
    async with Session() as session:
        await _reset(session)
        await save_profile(session, _profile())
        await session.commit()
        first = await load_profile(session, _CO)
        assert first is not None
        # re-saving exactly what we loaded must be a no-op in content
        await save_profile(session, first)
        await session.commit()
        second = await load_profile(session, _CO)

    assert second is not None
    assert without_connector_moves(first).slots == without_connector_moves(second).slots


async def test_disabled_source_stays_disabled_through_a_reload() -> None:
    async with Session() as session:
        await _reset(session)
        await save_profile(session, _profile())
        await session.commit()
        await set_source_enabled(session, _CO, "github", enabled=False)
        await session.commit()
        loaded = await load_profile(session, _CO)

    assert loaded is not None
    assert loaded.sources[0]["enabled"] is False
    assert loaded.moves.get("registry", {}) == {}, "a disabled source grants no actions"


# ---------------------------- per-source targets ----------------------------


def test_targeting_is_resolved_per_source() -> None:
    profile = with_connector_moves(_profile())
    target = extract_target(profile, "https://github.com/acme/repo/issues/42")
    assert target == {"repo": "acme/repo", "number": "42"}
    # a pull-request URL addresses the same issues API
    assert extract_target(profile, "https://github.com/acme/repo/pull/7") == {
        "repo": "acme/repo", "number": "7",
    }


def test_a_url_no_connected_source_recognises_yields_no_target() -> None:
    profile = with_connector_moves(_profile())
    assert extract_target(profile, "https://example.com/tickets/9") is None
    assert extract_target(profile, None) is None


def test_legacy_single_pattern_profiles_still_resolve() -> None:
    """Stored rows written before connectors declared capability are the system
    of record and are never rewritten — they must keep working."""
    legacy = _profile(
        sources=[{"source": "slack", "type": "message"}],  # no declared target
        moves={
            "target_url_pattern": r"example\.com/t/(\d+)",
            "target_params": ["number"],
        },
    )
    assert extract_target(legacy, "https://example.com/t/55") == {"number": "55"}


# --------------------------- learned vocabulary ---------------------------
#
# The replacement for `default_argument: triage`. Nothing declares which label
# to use; the engine reads the labels this company really uses.


async def _seed_event(session, eid: str, labels: list[str], assignee: str | None = None) -> None:
    await session.execute(
        text(
            """
            INSERT INTO events (id, company_id, source, type, actor_id, actor_name,
                                timestamp, content, metadata, raw, backfilled)
            VALUES (:id, :c, 'github', 'issue', 'x', 'x', now(), 'c',
                    CAST(:md AS jsonb), '{}'::jsonb, false)
            ON CONFLICT DO NOTHING
            """
        ),
        {
            "id": eid, "c": _CO,
            "md": json.dumps({
                "labels": [{"name": n} for n in labels],
                **({"assignee": assignee} if assignee else {}),
            }),
        },
    )


async def test_argument_vocabulary_is_read_from_real_events() -> None:
    async with Session() as session:
        await _reset(session)
        await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": _CO})
        await _seed_event(session, "e1", ["bug"])
        await _seed_event(session, "e2", ["bug", "enhancement"])
        await _seed_event(session, "e3", ["bug"])
        await session.commit()

        profile = with_connector_moves(_profile())
        labels = await learned_arguments(session, profile, "apply_label")

    # most-used first, and ONLY labels this company actually uses
    assert labels[0] == "bug"
    assert set(labels) == {"bug", "enhancement"}


async def test_nothing_observed_means_no_suggestion_not_a_guess() -> None:
    """The behaviour the whole module exists for: applying a label a repo has
    never used silently CREATES it, so an unknown vocabulary must stay empty."""
    async with Session() as session:
        await _reset(session)
        await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": _CO})
        await session.commit()

        profile = with_connector_moves(_profile())
        assert await learned_arguments(session, profile, "apply_label") == []
        # and the one-click default declines rather than inventing "triage"
        assert await default_argument(session, profile, "apply_label", _situation()) == ""


async def test_a_move_with_no_vocabulary_is_unaffected() -> None:
    async with Session() as session:
        await _reset(session)
        profile = with_connector_moves(_profile())
        assert await learned_arguments(session, profile, "close_issue") == []
