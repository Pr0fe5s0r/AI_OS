"""Rate limiting: the arithmetic, the route classification, and the two
decisions that make it safe in production.

The maths is separated from Redis on purpose. A limiter that can only be
tested by running one is a limiter whose boundary conditions never get tested
— and the boundary conditions are the whole point of choosing GCRA over a
fixed window.
"""

from __future__ import annotations

import inspect

from apps.api import authz, throttle
from packages.core import limits

# --------------------------------- the maths ---------------------------------


def test_a_fresh_bucket_allows_the_first_request():
    allowed, _tat, retry, _remaining = limits.gcra(1000.0, None, 60, 60)
    assert allowed and retry == 0.0


def test_a_burst_is_allowed_and_then_refused():
    """60/minute with a burst of 5 means five may arrive at once, not five per
    second forever."""
    now, tat = 1000.0, None
    allowed_count = 0
    for _ in range(20):
        allowed, tat, _retry, _rem = limits.gcra(now, tat, 60, 5)
        allowed_count += int(allowed)
    assert allowed_count == 5, "the burst is spent, then the drip rate applies"


def test_the_bucket_refills_with_time():
    now, tat = 1000.0, None
    for _ in range(5):
        _allowed, tat, _r, _rem = limits.gcra(now, tat, 60, 5)
    refused, tat_after_refusal, retry, _rem = limits.gcra(now, tat, 60, 5)
    assert not refused
    # Wait exactly as long as it said to, and the next one is allowed.
    allowed, _tat, _r, _rem = limits.gcra(now + retry, tat_after_refusal, 60, 5)
    assert allowed


def test_a_refusal_does_not_push_the_bucket_further_out():
    """Otherwise a client that retries hard locks itself out for longer with
    every attempt — the limiter would punish exactly the behaviour a 429 is
    trying to correct."""
    now, tat = 1000.0, None
    for _ in range(5):
        _a, tat, _r, _rem = limits.gcra(now, tat, 60, 5)
    before = tat
    for _ in range(10):
        allowed, tat, _r, _rem = limits.gcra(now, tat, 60, 5)
        assert not allowed
    assert tat == before


def test_there_is_no_window_boundary_to_double_up_on():
    """The reason this is GCRA and not a fixed window.

    A fixed window lets a caller spend a full window at 11:59:59 and another
    at 12:00:00 — twice the intended rate, at exactly the moment a retry storm
    arrives. Here, sixty requests spread either side of any instant can never
    exceed the rate plus the burst.
    """
    rate, burst = 60, 5
    tat = None
    allowed_total = 0
    # Two "windows" worth of time, requests hammering throughout.
    for step in range(120):
        for _ in range(3):
            allowed, tat, _r, _rem = limits.gcra(1000.0 + step, tat, rate, burst)
            allowed_total += int(allowed)
    # 120 seconds at 60/min is 120 permitted, plus at most one burst.
    assert allowed_total <= 120 + burst


def test_an_uncapped_class_always_allows():
    allowed, _tat, retry, _rem = limits.gcra(1000.0, None, 0, 0)
    assert allowed and retry == 0.0


def test_retry_after_is_never_zero():
    """A Retry-After of 0 tells a client to retry immediately, which is what it
    was just refused for."""
    assert limits._ceil_seconds(0.0) == 1
    assert limits._ceil_seconds(0.2) == 1
    assert limits._ceil_seconds(3.1) == 4


# ------------------------------- the limits -------------------------------


def test_the_expensive_class_is_the_one_with_a_concurrency_cap():
    """A rate of six a minute still permits six agentic answers AT ONCE, each
    holding three to five model calls open. The rate limit is about how fast
    work arrives; the cap is about how much is in flight."""
    assert limits.default_limit("agentic").concurrent > 0
    assert limits.default_limit("read").concurrent == 0


def test_expensive_classes_are_capped_harder_than_cheap_ones():
    assert limits.default_limit("agentic").per_minute < limits.default_limit("answer").per_minute
    assert limits.default_limit("answer").per_minute < limits.default_limit("read").per_minute


def test_a_key_with_no_override_gets_the_default():
    """Absence must not be read as a limit of zero."""
    assert limits.limit_for("read", None) == limits.default_limit("read")
    assert limits.limit_for("read", {}) == limits.default_limit("read")
    assert limits.limit_for("read", {"agentic_per_min": 2}) == limits.default_limit("read")


def test_a_per_key_override_wins():
    got = limits.limit_for("agentic", {"agentic_per_min": 60, "agentic_concurrent": 8})
    assert got.per_minute == 60 and got.concurrent == 8
    # And the burst rides with the rate rather than staying at the default,
    # which would leave a raised key with a burst below its own rate.
    assert got.burst >= got.per_minute


def test_the_workspace_ceiling_sits_above_the_key_limit():
    """Or an agency mints ten keys and the per-key limit means nothing."""
    assert limits.WORKSPACE_MULTIPLIER > 1


# --------------------------- classifying the routes ---------------------------


def test_every_route_the_app_serves_is_classified_or_exempt():
    """The audit. A route added next year is a CI failure rather than an
    unmetered hole — enforcement that depends on the next author remembering is
    not enforcement."""
    from apps.api.main import app

    unclassified = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or path.startswith("/api/auth/"):
            continue
        for method in getattr(route, "methods", set()) or set():
            if method in ("HEAD", "OPTIONS"):
                continue
            if throttle.classify(method, path) is None and (method, path) not in throttle._EXEMPT:
                unclassified.append(f"{method} {path}")
    assert not unclassified, f"unclassified routes: {unclassified}"


def test_an_agentic_answer_is_not_metered_as_a_hybrid_one():
    """Three to five model round-trips against one. Metering them from one
    bucket makes the limit either useless for one or punitive for the other."""
    assert throttle.classify("GET", "/api/answer", "hybrid") == "answer"
    assert throttle.classify("GET", "/api/answer", "agentic") == "agentic"
    # Agentic is the server's default, so an unspecified mode is the expensive
    # one — guessing cheap here would meter the costly path as the cheap one.
    assert throttle.classify("GET", "/api/answer", None) == "agentic"
    assert throttle.classify("GET", "/api/answer/stream", None) == "agentic"


def test_the_liveness_probe_is_never_throttled():
    """Throttling the health check is a way to make a healthy container report
    itself dead and be restarted."""
    assert throttle.classify("GET", "/api/health") is None


def test_uploads_are_metered_even_though_they_return_immediately():
    """The HTTP request is cheap and what follows it is not: a large PDF
    occupies a worker for 94-109 seconds after the 202."""
    assert throttle.classify("POST", "/api/items/file") == "ingest"


def test_cost_classes_are_not_permission_scopes():
    """They must not be made to line up. Reading a page picture is a cheap
    read; posting a file is cheap to serve and expensive afterwards."""
    assert authz.requirement("POST", "/api/items/file") == authz.WRITE
    assert throttle.classify("POST", "/api/items/file") == "ingest"
    assert authz.requirement("GET", "/api/answer") == authz.READ
    assert throttle.classify("GET", "/api/answer") == "agentic"


# ------------------------- the production decisions -------------------------


def test_the_limiter_fails_open():
    """The opposite of how authorisation fails, deliberately. Authorisation
    protects against a breach; a rate limiter protects against a noisy
    neighbour. Taking the API down because the limiter is unreachable trades a
    small risk for a total outage."""
    source = inspect.getsource(limits.Limiter.check)
    assert "except Exception" in source
    assert "degraded=type(exc).__name__" in source
    # And the allowance is reported rather than silent.
    assert "log.warning" in inspect.getsource(throttle.throttle)


def test_authorisation_still_fails_closed():
    """Guarding the contrast above: an unknown route is refused, not served."""
    assert "raise HTTPException(" in inspect.getsource(authz.authorise)
    assert authz.requirement("GET", "/api/not-a-route") is None


def test_every_decision_is_one_atomic_script():
    """Read-then-write in Python undercounts under a parallel worker pool —
    exactly when the counting matters. It is the bug that bit
    connector_health.record_failure once already."""
    assert "redis.call('SET'" in limits._GCRA_LUA
    assert "ZADD" in limits._SLOT_LUA and "ZCARD" in limits._SLOT_LUA
    # No Python-side read-modify-write of a bucket.
    source = inspect.getsource(limits.Limiter)
    assert ".get(" not in source or "register_script" in source


def test_a_concurrency_slot_expires_on_its_own():
    """A process killed mid-answer never releases its slot, and a leaked slot
    is capacity nobody can use again."""
    assert "ZREMRANGEBYSCORE" in limits._SLOT_LUA
    assert limits.SLOT_LEASE_SECONDS > 75, "longer than the slowest measured answer"


def test_the_slot_is_released_even_when_the_handler_raises():
    source = inspect.getsource(throttle.throttle)
    assert "finally:" in source and "release_slot" in source


def test_admission_only_never_mid_stream():
    """A half-written answer is worse than a clean refusal, and the caller has
    already been charged for the model calls behind it."""
    source = inspect.getsource(throttle.throttle)
    # The handler runs at the guarded yield — the one inside the try/finally.
    # (The earlier yields are the exempt short-circuits, which decide nothing.)
    handler_runs = source.index("try:\n        yield")
    assert source.index("limiter.check") < handler_runs
    assert source.index("take_slot") < handler_runs


def test_a_refusal_carries_retry_after_and_the_ratelimit_fields():
    refused = limits.Decision(False, 120, 0, 3, 3, "read", sustained=60)
    headers = refused.headers()
    assert headers["Retry-After"] == "3"
    # Sent on success too: a ceiling you can only find by hitting it is one no
    # client can pace itself against.
    allowed = limits.Decision(True, 120, 42, 0, 5, "read", sustained=60)
    assert "Retry-After" not in allowed.headers()
    assert allowed.headers()["RateLimit-Remaining"] == "42"


def test_the_reported_limit_is_in_the_same_units_as_remaining():
    """The first version answered `RateLimit-Limit: 60` beside
    `RateLimit-Remaining: 119`, seen live over HTTP. Remaining counts the burst
    budget, so the limit beside it has to be the burst — the per-minute rate is
    reported separately as a policy, in its own units."""
    headers = limits.Decision(True, 120, 119, 0, 120, "read", sustained=60).headers()
    assert int(headers["RateLimit-Remaining"]) <= int(headers["RateLimit-Limit"])
    assert headers["RateLimit-Policy"] == "60;w=60"


def test_a_signed_in_person_is_not_metered_as_the_workspace_key():
    """The console is a browser making many small reads. Sharing a bucket with
    a busy integration would let it lock a human out of their own console."""
    key = throttle._identity({"company_id": "w", "key_id": "k1"})
    session = throttle._identity({"company_id": "w", "user_id": "u1"})
    assert key is not None and session is not None
    assert key[0] != session[0]


def test_no_credential_means_nothing_to_meter():
    assert throttle._identity(None) is None
