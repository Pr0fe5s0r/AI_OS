from __future__ import annotations

import json

import packages.core.discovery as discovery_mod
from packages.core.discovery import (
    build_profile,
    deterministic_guess,
    induce_profile,
    inspect_payloads,
    validate_profile,
)

# Checkpoint 5 GOLDEN TEST for profile discovery.
#
# The fixture below is a fixed, realistic GitHub-shaped payload set — a test
# fixture living only inside pytest, on a throwaway company_id, per the
# project's no-mock-data rule.
#
# The point of a golden test here: inspect_payloads() is the deterministic
# half of discovery, so its output can be pinned exactly. The LLM half is
# stubbed, because the guarantee that matters isn't "the model names things
# nicely" — it's "whatever the model says, the profile we accept can actually
# normalize the payloads it was induced from".

_CO = "test-discovery"

_GITHUB_RAWS = [
    {
        "id": 2101,
        "number": 8,
        "title": "LLM chat UI is not working well",
        "state": "open",
        "user": {"login": "maya", "id": 55},
        "html_url": "https://github.com/acme/web/issues/8",
        "created_at": "2026-07-10T18:10:00Z",
        "updated_at": "2026-07-10T19:00:00Z",
        "closed_at": None,
        "body": "The chat panel renders but never streams a reply. Repro: open the app, send any message, watch it hang forever on the spinner.",
        "labels": [{"name": "bug"}],
        "assignee": None,
        "comments": 2,
        "_repo": "web",
        "_merged_at": None,
    },
    {
        "id": 2102,
        "number": 7,
        "title": "Add streaming responses",
        "state": "open",
        "user": {"login": "sam", "id": 56},
        "html_url": "https://github.com/acme/web/issues/7",
        "created_at": "2026-07-10T16:58:00Z",
        "updated_at": "2026-07-10T17:20:00Z",
        "closed_at": None,
        "body": "Users expect to see tokens as they are generated rather than waiting for the whole completion to finish first.",
        "labels": [],
        "assignee": None,
        "comments": 0,
        "_repo": "web",
        "_merged_at": None,
    },
    {
        "id": 2103,
        "number": 3,
        "title": "LLM api key issue",
        "state": "closed",
        "user": {"login": "maya", "id": 55},
        "html_url": "https://github.com/acme/web/issues/3",
        "created_at": "2026-07-05T11:31:00Z",
        "updated_at": "2026-07-05T12:00:00Z",
        "closed_at": "2026-07-05T12:00:00Z",
        "body": "The API key is not on a paid tier so every request fails with a quota error and the base url needs updating too.",
        "labels": [{"name": "bug"}],
        "assignee": None,
        "comments": 1,
        "_repo": "web",
        "_merged_at": None,
    },
]

_PROMPT = "inventory:\n{inventory}\n\nsamples:\n{samples}"


# ------------------------------- the golden shape -------------------------------


def test_golden_inspect_payloads_classifies_every_field() -> None:
    """Pinned: this exact payload shape must produce exactly these roles."""
    fields = inspect_payloads(_GITHUB_RAWS)["fields"]
    roles = {path: f["role"] for path, f in fields.items()}

    assert roles["number"] == "identifier"
    assert roles["id"] == "identifier"
    assert roles["created_at"] == "timestamp"
    assert roles["updated_at"] == "timestamp"
    assert roles["closed_at"] == "timestamp"  # values win: only sometimes set, still a date
    assert roles["state"] == "status"
    assert roles["title"] == "title"
    assert roles["body"] == "body"
    assert roles["html_url"] == "url"
    assert roles["user.login"] == "actor"  # nested under an actor-ish object
    assert roles["labels"] == "labels"
    assert roles["comments"] == "number"
    assert roles["assignee"] == "empty"  # null in every sample — nothing to learn


def test_golden_fill_rates_separate_creation_from_completion() -> None:
    """The signal that tells 'this happened' from 'this finished': a creation
    stamp is always set; a completion stamp only sometimes."""
    fields = inspect_payloads(_GITHUB_RAWS)["fields"]
    assert fields["created_at"]["fill_rate"] == 1.0
    assert 0 < fields["closed_at"]["fill_rate"] < 1.0
    assert fields["user.login"]["fill_rate"] == 1.0


def test_golden_deterministic_guess_picks_the_right_fields() -> None:
    inventory = inspect_payloads(_GITHUB_RAWS)
    guess = deterministic_guess(inventory, "github")

    assert guess["timestamp_field"] == "created_at"
    assert guess["end_field"] == "closed_at"  # the sometimes-set timestamp
    assert guess["status_field"] == "state"
    assert guess["actor_field"] == "user.login"
    assert guess["title_field"] == "title"
    assert guess["body_field"] == "body"
    assert guess["url_field"] == "html_url"
    assert "{" in guess["id_template"]


def test_inspect_payloads_is_pure_and_needs_no_llm() -> None:
    """No network, no DB, no model — the half we can pin down exactly."""
    first = inspect_payloads(_GITHUB_RAWS)
    second = inspect_payloads(_GITHUB_RAWS)
    assert first == second


# --------------------------- the guarantee that matters ---------------------------


def _stub_chat(payload: dict):
    def fake(messages, **kwargs):
        return json.dumps(payload)

    return fake


def test_induced_profile_normalizes_its_own_samples(monkeypatch) -> None:
    """The whole point: a profile discovery accepts must turn the very
    payloads it was induced from into real Events."""
    monkeypatch.setattr(
        discovery_mod,
        "chat",
        _stub_chat(
            {
                "thing_type": "Incident",
                "event_type": "issue",
                "id_template": "github-{number}",
                "timestamp_field": "created_at",
                "title_field": "title",
                "body_field": "body",
                "status_field": "state",
                "actor_field": "user.login",
                "url_field": "html_url",
                "end_field": "closed_at",
                "metadata_fields": ["state", "html_url", "number"],
                "rhythm_name": "issue_resolution_hours",
                "vocabulary": {"thing": "issue", "actor": "engineer"},
            }
        ),
    )

    profile, report = induce_profile(_CO, "github", "github", _GITHUB_RAWS, _PROMPT)

    assert report["valid"], report["errors"]
    assert report["used_llm"] is True
    # the model got to name things
    assert profile.things["types"][0]["name"] == "Incident"
    assert profile.things["types"][0]["event_type"] == "issue"
    assert profile.vocabulary["terms"]["thing"] == "issue"
    # it learned the rhythm from the sometimes-set completion stamp
    assert profile.rhythms[0]["name"] == "issue_resolution_hours"
    assert profile.rhythms[0]["end_field"] == "closed_at"
    # and it proposes NO moves — auto-inventing writes to a real system is unsafe
    assert profile.moves == {}


def test_induction_falls_back_to_the_deterministic_floor_when_the_model_is_down(monkeypatch) -> None:
    """No model, no problem: discovery still proposes something that works."""

    def boom(messages, **kwargs):
        raise RuntimeError("provider unreachable")

    monkeypatch.setattr(discovery_mod, "chat", boom)

    profile, report = induce_profile(_CO, "github", "github", _GITHUB_RAWS, _PROMPT)

    assert report["used_llm"] is False
    assert report["valid"], report["errors"]
    assert profile.sources[0]["source"] == "github"


def test_a_model_proposal_that_breaks_normalization_is_rejected(monkeypatch) -> None:
    """The model hallucinates an id template referencing a field that does not
    exist. Validation must catch it and fall back, rather than accept a
    profile that would quietly emit garbage events."""
    monkeypatch.setattr(
        discovery_mod,
        "chat",
        _stub_chat(
            {
                "thing_type": "Incident",
                "event_type": "issue",
                "id_template": "gh-{nonexistent_field}-{number}",  # will not format
                "timestamp_field": "created_at",
                "title_field": "title",
                "actor_field": "user.login",
            }
        ),
    )

    profile, report = induce_profile(_CO, "github", "github", _GITHUB_RAWS, _PROMPT)

    assert report["valid"], "must fall back to a profile that actually works"
    # the bad template was discarded in favour of the deterministic floor
    assert "nonexistent_field" not in json.dumps(profile.sources)


def test_validate_profile_catches_a_broken_mapping() -> None:
    """Validation is real: hand it a knowingly-bad proposal and it objects."""
    bad = build_profile(
        _CO, "github", "github",
        {"id_template": "github-{number}", "timestamp_field": "created_at", "title_field": "title"},
    )
    # blow the actor mapping away
    bad.sources[0]["mapping"]["actor_id"] = {"path": "does.not.exist", "default": "unknown"}
    errors = validate_profile(bad, "github", _GITHUB_RAWS)
    assert any("actor" in e for e in errors)


def test_induce_refuses_to_guess_from_nothing() -> None:
    """No payloads means no evidence — discovery must not invent a profile."""
    try:
        induce_profile(_CO, "github", "github", [], _PROMPT)
    except ValueError as exc:
        assert "at least one real payload" in str(exc)
    else:
        raise AssertionError("expected induce_profile to refuse an empty sample set")
