from __future__ import annotations

from datetime import UTC, datetime

from packages.core.profile import Profile, with_connector_mapping
from packages.core.resolve import _last_activity
from packages.shared.schema import Actor, Event

# "Still moving" has to mean moving, not young.
#
# Every stall watcher hangs off a Thing's `last_activity`, and resolve() set it
# from `event.timestamp` — which the induced mapping fills from the record's
# CREATION date. So "opened, then never touched again" was really measuring
# "opened a while ago": a pull request edited an hour ago aged at exactly the
# same rate as one nobody had opened since, and editing it could never clear
# the flag. The engine's own primer says age since last activity matters more
# than age since creation; it was measuring the thing it says not to.

CREATED = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
UPDATED = datetime(2026, 7, 22, 19, 9, tzinfo=UTC)


def _event(metadata: dict) -> Event:
    return Event(
        id="10", company_id="test-activity", source="github", type="pull_request",
        actor=Actor(id="u", name="u"), timestamp=CREATED,
        content="Add README", metadata=metadata, raw={},
    )


def test_a_record_edited_today_counts_as_active_today() -> None:
    activity = _last_activity(
        _event({"updated_at": "2026-07-22T19:09:00Z"}), {"activity_field": "updated_at"}
    )
    assert activity == UPDATED
    assert activity != CREATED


def test_without_a_declared_field_it_falls_back_to_the_event_time() -> None:
    """A source with no "last changed" stamp is not a failure — creation is
    then genuinely the best evidence available."""
    assert _last_activity(_event({"updated_at": "2026-07-22T19:09:00Z"}), {}) == CREATED


def test_a_missing_or_malformed_stamp_never_loses_the_event() -> None:
    """This runs inside the resolve job; raising here would fail the whole
    pipeline for one bad date."""
    cfg = {"activity_field": "updated_at"}
    assert _last_activity(_event({}), cfg) == CREATED
    assert _last_activity(_event({"updated_at": ""}), cfg) == CREATED
    assert _last_activity(_event({"updated_at": "not-a-date"}), cfg) == CREATED
    assert _last_activity(_event({"updated_at": None}), cfg) == CREATED


def test_a_naive_stamp_is_treated_as_utc_not_left_ambiguous() -> None:
    """Comparing a naive datetime against an aware one raises, and it would
    raise inside the watcher rather than here."""
    activity = _last_activity(
        _event({"updated_at": "2026-07-22T19:09:00"}), {"activity_field": "updated_at"}
    )
    assert activity.tzinfo is not None
    assert activity == UPDATED


def test_an_already_connected_workspace_gets_this_without_re_learning() -> None:
    """The profile was discovered before activity_field existed. Requiring a
    re-discovery would leave every existing workspace measuring the wrong thing
    with nothing on screen to suggest it."""
    stale = Profile(
        company_id="test-activity",
        sources=[{"source": "github", "kind": "connector", "mapping": {"content": {"path": "title"}}}],
        things={"status_field": "state"},
    )
    assert with_connector_mapping(stale).things["activity_field"] == "updated_at"


def test_a_decision_already_in_the_profile_is_never_overwritten() -> None:
    """Read-time defaults fill gaps; they do not overrule what discovery
    induced or a person set."""
    chosen = Profile(
        company_id="test-activity",
        sources=[{"source": "github", "kind": "connector", "mapping": {}}],
        things={"activity_field": "last_edited_at"},
    )
    assert with_connector_mapping(chosen).things["activity_field"] == "last_edited_at"
