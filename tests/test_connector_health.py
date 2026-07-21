from __future__ import annotations

import asyncio

from sqlalchemy import text

from apps.common.health import _evaluate_one
from packages.connectors.base import field_presence, validate_raw
from packages.core import connector_health as ch
from packages.core.db import Session
from packages.core.pipeline import ingest_raw

_CO = "test-connector-health"

_GOOD_ISSUE = {
    "number": 501,
    "title": "Checkout 500 at payment",
    "state": "open",
    "user": {"login": "maya"},
    "html_url": "https://github.com/acme/web/issues/501",
    "created_at": "2026-07-14T10:00:00Z",
    "body": "Stripe charge created but order never marked paid.",
    "labels": [],
    "_repo": "web",
    "_merged_at": None,
}

# Missing "number" and "html_url" — the two fields a real API change or a
# proxy mangling the response would most plausibly drop.
_MALFORMED_ISSUE = {
    "title": "???",
    "state": "open",
    "user": {"login": "maya"},
    "created_at": "2026-07-14T10:05:00Z",
}

_SOURCE_CONFIG = {
    "source": "github",
    "company_id": _CO,
    "connector_type": "github",
    "context": {"repo": "web"},
    "mapping": {
        "id": {"template": "gh-{repo}-{number}"},
        "type": {"const": "issue"},
        "actor_id": {"path": "user.login", "default": "unknown"},
        "actor_name": {"path": "user.login", "default": "unknown"},
        "timestamp": {"path": "created_at"},
        "content": {"path": "title", "default": ""},
        "metadata": {"number": {"path": "number"}, "url": {"path": "html_url"}},
    },
}


class _FakeRedis:
    """Stubs only the arq transport boundary — everything under test is real."""

    async def enqueue_job(self, *args, **kwargs):
        return None


async def _reset(session) -> None:
    await session.execute(text("DELETE FROM events WHERE company_id = :c"), {"c": _CO})
    await session.execute(
        text("DELETE FROM connector_health WHERE company_id = :c"), {"c": _CO}
    )
    await session.commit()


# --------------------------- schema validation (pure) ---------------------------


def test_malformed_github_payload_fails_schema_validation() -> None:
    error = validate_raw("github", _MALFORMED_ISSUE)
    assert error is not None
    assert "number" in error


def test_wellformed_github_payload_passes_schema_validation() -> None:
    assert validate_raw("github", _GOOD_ISSUE) is None


def test_unknown_connector_kind_skips_validation_rather_than_erroring() -> None:
    assert validate_raw("some-future-connector", {"anything": True}) is None


def test_field_presence_flags_missing_fields() -> None:
    presence = field_presence("github", _MALFORMED_ISSUE)
    assert presence["number"] is False
    assert presence["html_url"] is False
    assert presence["title"] is True


# ------------------------- ingest_raw: the actual pipeline -------------------------


async def test_malformed_payload_does_not_crash_and_does_not_store_an_event() -> None:
    async with Session() as session:
        await _reset(session)

    ctx = {"redis": _FakeRedis()}
    event_id = await ingest_raw(ctx, _SOURCE_CONFIG, _MALFORMED_ISSUE, resolve_cfg={})
    assert event_id == ""  # no crash — a quiet, well-typed no-op

    async with Session() as session:
        n_events = (
            await session.execute(
                text("SELECT count(*) FROM events WHERE company_id = :c"), {"c": _CO}
            )
        ).scalar_one()
        assert n_events == 0, "a malformed payload must never reach the store"

        health = await ch.get_health_one(session, _CO, "github")
        assert health is not None
        assert health["consecutive_failures"] == 1
        assert health["last_schema_error"] and "number" in health["last_schema_error"]


async def test_a_wellformed_payload_after_failures_resets_the_streak() -> None:
    async with Session() as session:
        await _reset(session)

    ctx = {"redis": _FakeRedis()}
    await ingest_raw(ctx, _SOURCE_CONFIG, _MALFORMED_ISSUE, resolve_cfg={})
    await ingest_raw(ctx, _SOURCE_CONFIG, _MALFORMED_ISSUE, resolve_cfg={})

    async with Session() as session:
        health = await ch.get_health_one(session, _CO, "github")
        assert health["consecutive_failures"] == 2

    event_id = await ingest_raw(ctx, _SOURCE_CONFIG, _GOOD_ISSUE, resolve_cfg={})
    assert event_id == "gh-web-501"

    async with Session() as session:
        health = await ch.get_health_one(session, _CO, "github")
        assert health["consecutive_failures"] == 0
        assert health["last_success_at"] is not None

        n_events = (
            await session.execute(
                text("SELECT count(*) FROM events WHERE company_id = :c"), {"c": _CO}
            )
        ).scalar_one()
        assert n_events == 1


async def test_three_consecutive_failures_flip_status_to_degraded_on_the_next_health_pass() -> None:
    """The ingest-time hooks only accumulate signal; only the health cron's
    evaluation decides `status` — this proves that hand-off end to end."""
    async with Session() as session:
        await _reset(session)

    ctx = {"redis": _FakeRedis()}
    for _ in range(3):
        await ingest_raw(ctx, _SOURCE_CONFIG, _MALFORMED_ISSUE, resolve_cfg={})

    async with Session() as session:
        before = await ch.get_health_one(session, _CO, "github")
        assert before["status"] == "healthy", "status must not move until the cron evaluates it"

        result = await _evaluate_one(session, _CO, "github")
        await session.commit()
        assert result["status"] == "degraded"

        after = await ch.get_health_one(session, _CO, "github")
        assert after["status"] == "degraded"


# ---------------------- concurrent failures must not lose updates ----------------------


async def test_concurrent_failures_all_land_no_lost_updates() -> None:
    """Regression test: arq runs ingest_raw jobs concurrently, so record_failure
    must be an atomic SQL increment. A read-then-write in Python here loses
    updates — confirmed by hand: 3 concurrent malformed payloads against the
    live worker only advanced the streak by 1 before this was atomic."""
    async with Session() as session:
        await _reset(session)

    async def _fail(i: int) -> None:
        async with Session() as session:
            await ch.record_failure(session, "github", _CO, f"synthetic error {i}")
            await session.commit()

    await asyncio.gather(*(_fail(i) for i in range(10)))

    async with Session() as session:
        health = await ch.get_health_one(session, _CO, "github")
        assert health["consecutive_failures"] == 10


# ------------------------------- field-completeness drop -------------------------------


def test_completeness_drop_flags_a_field_whose_rate_fell_off() -> None:
    fc = {
        "current": {"assignee": 0.1, "labels": 0.9},
        "baseline": {"assignee": 0.85, "labels": 0.88},
    }
    assert ch.completeness_drop(fc) == ["assignee"]


def test_completeness_drop_ignores_fields_still_near_their_history() -> None:
    fc = {"current": {"labels": 0.9}, "baseline": {"labels": 0.92}}
    assert ch.completeness_drop(fc) == []
