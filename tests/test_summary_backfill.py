"""Every document gets a card, and nobody pays for the same card twice.

Cards became load-bearing when retrieval started routing on them: on a store
larger than the catalogue window, the card is what decides which documents a
question is allowed to see (``packages.core.routing``). So "most documents have
a card" is not good enough — a document without one competes on the weaker
signal, and on a large store that can mean it is never considered.

The cost is what makes this delicate. Summarising is one model call per section
plus one for the card — about thirteen for a twelve-section circular, not one.
The endpoint that existed before re-ran all of them for every document every
time it was called: roughly 1,300 calls to regenerate a hundred documents'
summaries that were already correct.
"""

from __future__ import annotations

import pytest

from apps.common import summaries
from packages.core.pipeline import _summaries_enabled


def test_summaries_are_on_by_default(monkeypatch):
    """It defaults ON, which it did not before. As an optional catalogue nicety
    that was fine; as the input to a routing decision it is not — a store with
    no cards routes on the weaker signal and nothing tells anyone."""
    monkeypatch.delenv("SUMMARIES_ENABLED", raising=False)
    assert _summaries_enabled() is True
    assert summaries.enabled() is True

    # And it stays switchable, because the cost is real.
    monkeypatch.setenv("SUMMARIES_ENABLED", "false")
    assert _summaries_enabled() is False
    assert summaries.enabled() is False


def test_the_two_gates_agree(monkeypatch):
    """Backfilling old documents while skipping new uploads would be incoherent
    — either the store keeps cards or it does not. One flag, both paths."""
    for value, expected in (("true", True), ("false", False), ("1", True), ("no", False)):
        monkeypatch.setenv("SUMMARIES_ENABLED", value)
        assert _summaries_enabled() is expected
        assert summaries.enabled() is expected


def test_the_backfill_is_bounded(monkeypatch):
    """A fresh hundred-document store would otherwise queue about 1,300 model
    calls on one tick and starve live ingestion of the same rate limit."""
    assert summaries.BATCH > 0
    assert summaries.BATCH <= 50, "a pass that queues the whole store is a stampede"


def test_the_interval_is_slow_and_cannot_be_misconfigured_to_zero(monkeypatch):
    """Catch-up work, competing with live ingestion for one rate limit. A
    zero or negative interval read from the environment would turn a background
    pass into a busy loop."""
    monkeypatch.delenv("SUMMARIES_INTERVAL_SECONDS", raising=False)
    assert summaries.interval_seconds() >= 60

    monkeypatch.setenv("SUMMARIES_INTERVAL_SECONDS", "0")
    assert summaries.interval_seconds() >= 60
    monkeypatch.setenv("SUMMARIES_INTERVAL_SECONDS", "not a number")
    assert summaries.interval_seconds() >= 60


@pytest.mark.asyncio
async def test_a_disabled_store_queues_nothing():
    """The pass must be a no-op when summaries are off, not merely harmless."""
    import os

    old = os.environ.get("SUMMARIES_ENABLED")
    os.environ["SUMMARIES_ENABLED"] = "false"
    try:
        result = await summaries.backfill_all({"redis": object()})
        assert "skipped" in result
    finally:
        if old is None:
            os.environ.pop("SUMMARIES_ENABLED", None)
        else:
            os.environ["SUMMARIES_ENABLED"] = old


@pytest.mark.asyncio
async def test_a_pass_without_a_queue_does_not_pretend_to_work(monkeypatch):
    """Enqueuing is the entire job. A pass that cannot enqueue has done nothing
    and must say so, rather than returning a zero that reads like "all done"."""
    monkeypatch.setenv("SUMMARIES_ENABLED", "true")
    result = await summaries.backfill_all({})
    assert "skipped" in result


@pytest.mark.asyncio
async def test_two_passes_never_overlap(monkeypatch):
    """Same guard consolidation and the mapper use. Two passes would queue the
    same documents twice, and every duplicate is paid for in model calls."""
    monkeypatch.setenv("SUMMARIES_ENABLED", "true")
    async with summaries._running:
        result = await summaries.backfill_all({"redis": object()})
    assert "already running" in result["skipped"]


def test_the_default_rebuilds_nothing():
    """The regression this exists to prevent. `summarize_now` passed force=True
    for every document on every call; gap-filling has to be what happens when
    nobody asks for anything in particular, because the expensive behaviour
    must be the one you opt into."""
    import inspect

    from apps.api.main import summarize_now

    rebuild = inspect.signature(summarize_now).parameters["rebuild"]
    assert rebuild.default.default is False, "a rebuild must be asked for, never assumed"
