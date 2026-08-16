"""The limiter against a real Redis.

The arithmetic is covered in tests/test_rate_limits.py without a server. What
cannot be covered there is everything that only exists once Redis is involved:
whether the Lua actually runs, whether two callers share a bucket they should
not, whether a slot is really reclaimed, and whether an unreachable Redis
allows the request instead of refusing it.

Skipped, not failed, where no Redis is reachable — and gated on REDIS rather
than on the `needs_db` marker, because none of this touches Postgres and
borrowing that marker would skip these wherever a database happened to be
missing while a perfectly good Redis was running.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from packages.core.limits import Limit, Limiter

_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


def _reachable() -> bool:
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(_URL)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 6379), 1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason=f"needs Redis at {_URL} (docker compose up redis)"
)


async def _limiter() -> Limiter:
    from redis.asyncio import Redis

    return Limiter(Redis.from_url(_URL))


def _who() -> str:
    # A fresh identity per test: buckets are keyed by caller, and a test that
    # inherits another test's bucket fails for reasons that have nothing to do
    # with what it is checking.
    return f"test:{uuid.uuid4().hex}"


async def test_the_bucket_actually_refuses_over_redis():
    limiter = await _limiter()
    limit = Limit(per_minute=60, burst=3)
    key, workspace = _who(), _who()

    allowed = 0
    for _ in range(10):
        decision = await limiter.check(
            key_id=key, workspace_id=workspace, cls="read", limit=limit
        )
        allowed += int(decision.allowed)
    assert allowed == 3, "the burst is spent and the rest are refused"

    refused = await limiter.check(
        key_id=key, workspace_id=workspace, cls="read", limit=limit
    )
    assert not refused.allowed
    assert refused.retry_after >= 1, "never zero, or the client retries at once"
    await limiter.close()


async def test_two_keys_do_not_share_a_bucket():
    limiter = await _limiter()
    limit = Limit(per_minute=60, burst=2)
    workspace = _who()
    a, b = _who(), _who()

    for _ in range(3):
        await limiter.check(key_id=a, workspace_id=workspace, cls="read", limit=limit)
    # a is now spent; b has not made a single request.
    decision = await limiter.check(
        key_id=b, workspace_id=workspace, cls="read", limit=limit
    )
    assert decision.allowed
    await limiter.close()


async def test_the_classes_are_separate_buckets():
    """Exhausting agentic answers must not stop the caller searching."""
    limiter = await _limiter()
    key, workspace = _who(), _who()
    tight = Limit(per_minute=60, burst=1)

    await limiter.check(key_id=key, workspace_id=workspace, cls="agentic", limit=tight)
    spent = await limiter.check(
        key_id=key, workspace_id=workspace, cls="agentic", limit=tight
    )
    assert not spent.allowed

    still_reading = await limiter.check(
        key_id=key, workspace_id=workspace, cls="read", limit=tight
    )
    assert still_reading.allowed
    await limiter.close()


async def test_the_workspace_ceiling_catches_many_keys():
    """One agency minting ten keys must not multiply its own limit by ten."""
    limiter = await _limiter()
    workspace = _who()
    limit = Limit(per_minute=60, burst=2)

    allowed = 0
    for _ in range(20):
        # A different key every time — each has its own untouched bucket, so
        # only the workspace ceiling can stop this.
        decision = await limiter.check(
            key_id=_who(), workspace_id=workspace, cls="read", limit=limit
        )
        allowed += int(decision.allowed)
    assert allowed < 20, "the ceiling above the per-key limits did nothing"
    await limiter.close()


async def test_concurrency_slots_are_taken_and_released():
    limiter = await _limiter()
    key = _who()
    limit = Limit(per_minute=600, burst=600, concurrent=2)

    first = await limiter.take_slot(key_id=key, cls="agentic", limit=limit)
    second = await limiter.take_slot(key_id=key, cls="agentic", limit=limit)
    third = await limiter.take_slot(key_id=key, cls="agentic", limit=limit)
    assert first and second, "two slots were available"
    assert third is None, "the third must be refused, not queued"

    await limiter.release_slot(key_id=key, cls="agentic", token=first)
    again = await limiter.take_slot(key_id=key, cls="agentic", limit=limit)
    assert again, "releasing a slot returns the capacity"
    await limiter.close()


async def test_a_class_with_no_cap_never_takes_a_slot():
    limiter = await _limiter()
    key = _who()
    uncapped = Limit(per_minute=60, burst=60, concurrent=0)
    for _ in range(5):
        assert await limiter.take_slot(key_id=key, cls="read", limit=uncapped) == ""
    await limiter.close()


async def test_the_count_survives_concurrent_callers():
    """The reason every decision is one Lua script.

    Read-then-write in Python undercounts under parallelism — which is exactly
    when a limit is being approached. Twenty simultaneous requests against a
    burst of 5 must let through 5, not 20.
    """
    limiter = await _limiter()
    key, workspace = _who(), _who()
    limit = Limit(per_minute=60, burst=5)

    decisions = await asyncio.gather(
        *(
            limiter.check(key_id=key, workspace_id=workspace, cls="read", limit=limit)
            for _ in range(20)
        )
    )
    assert sum(d.allowed for d in decisions) == 5
    await limiter.close()


async def test_an_unreachable_redis_allows_the_request():
    """Fail open, and say so. A limiter outage must not become an API outage —
    it guards against a noisy neighbour, not against a breach."""
    from redis.asyncio import Redis

    limiter = Limiter(Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=1))
    decision = await limiter.check(
        key_id=_who(), workspace_id=_who(), cls="read", limit=Limit(1, 1)
    )
    assert decision.allowed
    assert decision.degraded, "the allowance must be reported, not silent"

    # And a slot request degrades the same way rather than blocking the call.
    assert await limiter.take_slot(key_id=_who(), cls="agentic", limit=Limit(1, 1, 2)) == ""
