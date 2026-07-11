from __future__ import annotations

import pytest

from verticals.software.scheduler import (
    ingest_timeout,
    scan_enabled,
    scan_interval,
    scan_minutes,
    scheduled_scan,
)


def test_scan_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCAN_ENABLED", raising=False)
    assert scan_enabled() is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", ""])
def test_scan_can_be_switched_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SCAN_ENABLED", value)
    assert scan_enabled() is False


def test_a_disabled_scan_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No DB, no Redis, no email — the guard must come first."""
    monkeypatch.setenv("SCAN_ENABLED", "false")
    import asyncio

    assert asyncio.run(scheduled_scan({})) == {"skipped": "SCAN_ENABLED=false"}


@pytest.mark.parametrize(
    ("interval", "expected"),
    [
        ("15", {0, 15, 30, 45}),
        ("30", {0, 30}),
        ("60", {0}),
        ("5", {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
    ],
)
def test_interval_becomes_minutes_past_the_hour(
    monkeypatch: pytest.MonkeyPatch, interval: str, expected: set[int]
) -> None:
    monkeypatch.setenv("SCAN_INTERVAL_MINUTES", interval)
    assert scan_minutes() == expected


@pytest.mark.parametrize(("value", "expected"), [("0", 1), ("-5", 1), ("999", 60)])
def test_a_nonsense_interval_is_clamped(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: int
) -> None:
    """An interval of 0 would mean a scan every minute forever; 999 would never fire."""
    monkeypatch.setenv("SCAN_INTERVAL_MINUTES", value)
    assert scan_interval() == expected


def test_garbage_config_falls_back_instead_of_crashing_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # a typo in .env must not stop the worker from booting at all
    monkeypatch.setenv("SCAN_INTERVAL_MINUTES", "every 15 min")
    monkeypatch.setenv("SCAN_INGEST_TIMEOUT", "two minutes")
    assert scan_interval() == 15
    assert ingest_timeout() == 120.0
