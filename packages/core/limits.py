from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# RATE LIMITING.
#
# The unit is not "requests". In this store a request costs anywhere between
# two database queries and five model round-trips, so one number covering all
# of them protects nothing: a limit generous enough for search lets a tenant
# spend an entire provider quota on agentic answers, and a limit tight enough
# for agentic answers makes ordinary search unusable. Measured on this
# codebase:
#
#   search          1 embedding call + 2 queries        tens of ms
#   answer hybrid   the above + 1 chat call
#   answer agentic  3-5 chat round-trips, sometimes     38-75s
#                   a vision call
#   ingest          returns in milliseconds and then    94-109s of worker
#                   occupies a worker                   time on a large PDF
#
# So a caller has one bucket per COST CLASS, and the expensive class has a
# second control the buckets cannot provide: a cap on how many may run AT
# ONCE. A rate of six agentic answers a minute still permits six of them
# simultaneously, each holding several model calls open — the rate limit is
# about how fast work arrives, the concurrency cap about how much is in flight.
#
# Two properties this file is built around:
#
#   ATOMIC. Every decision is one Lua script, one round trip. Read-then-write
#   in Python is wrong under a worker pool that runs jobs in parallel — it
#   undercounts exactly when the counting matters, which is the bug that bit
#   connector_health.record_failure once already.
#
#   FAILS OPEN. If Redis cannot be reached the request is ALLOWED. This is the
#   opposite of how authorisation fails, and deliberately so: authorisation
#   protects against a breach, a rate limiter protects against a noisy
#   neighbour. Taking the whole API down because the limiter is unreachable
#   trades a small risk for a total outage. The caller is let through and the
#   reason is reported, so the failure surfaces rather than passing silently.
# ---------------------------------------------------------------------------


# The cost classes. Named for what the caller is doing, not for the routes,
# because the route table that maps onto these lives in the API layer where
# routes are known (packages/core imports nothing from apps/*).
CLASSES = ("read", "answer", "agentic", "ingest", "admin")

# How long a concurrency slot may be held before it is assumed abandoned. A
# process killed mid-answer never releases its slot, and a leaked slot is
# permanent capacity loss, so slots expire on their own. Comfortably longer
# than the slowest answer measured (75s) plus the answer deadline.
SLOT_LEASE_SECONDS = 300


@dataclass(frozen=True, slots=True)
class Limit:
    """One bucket. `burst` is how far above the steady rate a caller may spike
    before being refused, `concurrent` how many may be in flight at once (0
    meaning uncapped — only the expensive classes need it)."""

    per_minute: int
    burst: int
    concurrent: int = 0

    @property
    def uncapped(self) -> bool:
        return self.per_minute <= 0


# Set from the traffic actually recorded in `query_traces`, not from taste.
# Across 493 real queries the busiest minute any workspace has ever had was 13
# requests, and the p95 active minute was 7 — so `read` at 60/min sits roughly
# five times above the busiest minute this deployment has ever seen, and a
# single-tenant install will never meet the limiter at all. That is the point:
# self-hosting is the primary deployment model, and these bite only in the
# multi-agency case they exist for.
#
# `agentic` is the exception worth understanding. An agentic answer takes
# 38-75s, so a client calling them one after another cannot exceed about 1.5 a
# minute; with the concurrency cap of 2 the sustainable ceiling is around 3.
# Six a minute is therefore already double what a well-behaved caller can
# reach, and what it actually stops is a parallel fan-out or a retry storm.
# The CONCURRENCY cap, not the rate, is the control doing the work here.
_FALLBACK: dict[str, Limit] = {
    "read": Limit(per_minute=60, burst=120),
    "answer": Limit(per_minute=20, burst=30),
    "agentic": Limit(per_minute=6, burst=8, concurrent=2),
    "ingest": Limit(per_minute=30, burst=60),
    "admin": Limit(per_minute=30, burst=45),
}

# A workspace ceiling above the per-key limits, or one agency mints ten keys
# and the per-key limit means nothing. Expressed as a multiple so raising a
# key's limit raises the ceiling with it.
WORKSPACE_MULTIPLIER = 3.0


def enabled() -> bool:
    """Off leaves every code path intact and every request allowed, so a
    deployment that does not want limiting is not running a limiter that
    happens to permit everything."""
    return os.getenv("RATE_LIMIT_ENABLED", "true").lower() not in ("0", "false", "no")


def _env_int(name: str, fallback: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def default_limit(cls: str) -> Limit:
    """The limit for a class before any per-key override, read from the
    environment so an operator can retune without a deploy."""
    base = _FALLBACK.get(cls) or _FALLBACK["read"]
    upper = cls.upper()
    return Limit(
        per_minute=_env_int(f"RATE_LIMIT_{upper}_PER_MIN", base.per_minute),
        burst=_env_int(f"RATE_LIMIT_{upper}_BURST", base.burst),
        concurrent=_env_int(f"RATE_LIMIT_{upper}_CONCURRENT", base.concurrent),
    )


def limit_for(cls: str, overrides: dict[str, Any] | None = None) -> Limit:
    """The limit that applies to one caller for one class.

    A per-key override wins over the environment default, which is what lets
    one noisy agency be tightened — or one trusted integration be raised —
    without moving everybody else. An absent entry means "use the default", so
    a key with no opinion is not a key with a limit of zero.

    Overrides arrive as the api_keys.rate_limits object, e.g.
    ``{"agentic_per_min": 12, "agentic_concurrent": 4}``.
    """
    base = default_limit(cls)
    if not overrides:
        return base
    per_minute = overrides.get(f"{cls}_per_min")
    concurrent = overrides.get(f"{cls}_concurrent")
    if per_minute is None and concurrent is None:
        return base
    rate = base.per_minute if per_minute is None else int(per_minute)
    # The burst rides with the rate rather than being configured separately:
    # two dials per class per key is a configuration surface nobody will keep
    # coherent, and a burst below its own rate is never what anyone meant.
    burst = max(rate, round(rate * (base.burst / base.per_minute))) if base.per_minute else rate
    return Limit(
        per_minute=rate,
        burst=burst,
        concurrent=base.concurrent if concurrent is None else int(concurrent),
    )


# --------------------------------- the maths ---------------------------------
#
# GCRA (a leaky bucket used as a meter), not a fixed window. A fixed window
# lets a caller spend a full window at 11:59:59 and another at 12:00:00 —
# double the intended rate at the boundary, which is exactly when a retry storm
# arrives. GCRA holds one number per bucket, the "theoretical arrival time" of
# the next permitted request, and reads a precise Retry-After straight off it.


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    # The capacity a caller may spend RIGHT NOW — the burst, not the per-minute
    # rate. It has to be the burst because `remaining` is measured against the
    # same budget, and the first version reported `limit: 60` beside
    # `remaining: 119`, which is not a limit anybody can pace against. The
    # sustained rate is reported separately, in RateLimit-Policy.
    limit: int
    remaining: int
    # Seconds until the caller may retry (when refused) or until the bucket is
    # empty again (when allowed). Whole seconds, rounded up, never zero when
    # refused — a Retry-After of 0 invites an immediate retry.
    retry_after: int
    reset: int
    cls: str
    # The per-minute rate behind the burst, reported so a client can pace its
    # steady state rather than only its next request.
    sustained: int = 0
    # Set when the decision was not really made: the limiter was unreachable
    # and the request was allowed through. Never a reason to refuse.
    degraded: str | None = None

    def headers(self) -> dict[str, str]:
        """The IETF RateLimit fields, so a client can pace itself instead of
        discovering the wall. Sent on success as well as refusal — a limit you
        only learn about by exceeding it is one you cannot design around."""
        out = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(max(0, self.remaining)),
            "RateLimit-Reset": str(self.reset),
        }
        if self.sustained:
            # Both numbers, in the units each is measured in: how much may be
            # spent at once, and the rate it refills at.
            out["RateLimit-Policy"] = f"{self.sustained};w=60"
        if not self.allowed:
            out["Retry-After"] = str(self.retry_after)
        return out


def gcra(
    now: float, tat: float | None, rate_per_minute: int, burst: int
) -> tuple[bool, float, float, int]:
    """One GCRA step, as pure arithmetic so it can be tested without a Redis.

    Returns (allowed, new_tat, retry_after_seconds, remaining). `tat` is the
    stored theoretical arrival time; None means the bucket has never been used.
    """
    if rate_per_minute <= 0:
        return True, now, 0.0, 0

    emission = 60.0 / rate_per_minute
    tolerance = emission * max(1, burst)
    arrival = now if tat is None else max(tat, now)
    new_tat = arrival + emission
    allow_at = new_tat - tolerance

    if now < allow_at:
        # Refused: the bucket keeps its old value. A refused request must not
        # push the arrival time further out, or a client that retries hard
        # would lock itself out for longer with every attempt.
        remaining = 0
        return False, (tat if tat is not None else now), allow_at - now, remaining

    remaining = int(max(0.0, (tolerance - (new_tat - now)) / emission))
    return True, new_tat, 0.0, remaining


# The same arithmetic, server-side and atomic. KEYS[1] is the bucket.
# ARGV: now, emission, tolerance, ttl.
_GCRA_LUA = """
local tat = tonumber(redis.call('GET', KEYS[1]))
local now = tonumber(ARGV[1])
local emission = tonumber(ARGV[2])
local tolerance = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local arrival = now
if tat and tat > now then arrival = tat end
local new_tat = arrival + emission
local allow_at = new_tat - tolerance
if now < allow_at then
  return {0, tostring(allow_at - now), '0'}
end
redis.call('SET', KEYS[1], tostring(new_tat), 'PX', ttl)
local remaining = math.floor((tolerance - (new_tat - now)) / emission)
if remaining < 0 then remaining = 0 end
return {1, '0', tostring(remaining)}
"""

# Concurrency slots as a sorted set scored by the time they were taken, so a
# slot whose holder died is reclaimed by the next caller instead of leaking
# capacity forever. KEYS[1] the set; ARGV: now, lease, cap, token.
_SLOT_LUA = """
local now = tonumber(ARGV[1])
local lease = tonumber(ARGV[2])
local cap = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - lease)
local held = redis.call('ZCARD', KEYS[1])
if held >= cap then
  return {0, tostring(held)}
end
redis.call('ZADD', KEYS[1], now, ARGV[4])
redis.call('PEXPIRE', KEYS[1], math.ceil(lease * 1000))
return {1, tostring(held + 1)}
"""


def bucket_key(scope_kind: str, identity: str, cls: str) -> str:
    return f"ratelimit:{scope_kind}:{identity}:{cls}"


def slot_key(identity: str, cls: str) -> str:
    return f"ratelimit:slots:{identity}:{cls}"


# ------------------------------- the front door -------------------------------


class Limiter:
    """Holds the Redis connection and the compiled scripts.

    One instance per process, created at startup. Every method answers a
    Decision and none of them raise: an unreachable limiter allows the request
    and says so in `degraded`.
    """

    def __init__(self, redis: Any) -> None:
        self._redis = redis
        self._gcra: Any | None = None
        self._slots: Any | None = None

    async def close(self) -> None:
        # redis-py renamed close() to aclose() in 5.0.1 and deprecated the old
        # name; arq still ships the older shape, so ask for whichever exists.
        closer = getattr(self._redis, "aclose", None) or self._redis.close
        await closer()

    def _scripts(self) -> tuple[Any, Any]:
        if self._gcra is None or self._slots is None:
            self._gcra = self._redis.register_script(_GCRA_LUA)
            self._slots = self._redis.register_script(_SLOT_LUA)
        return self._gcra, self._slots

    async def check(
        self, *, key_id: str, workspace_id: str, cls: str, limit: Limit
    ) -> Decision:
        """Both buckets: the caller's key, and the workspace ceiling above it.

        The key is checked first, so a caller who is over their own limit is
        told that rather than being told the workspace is busy — the tighter
        bound is the useful one to report.
        """
        if limit.uncapped or not enabled():
            return Decision(True, 0, 0, 0, 0, cls)

        now = time.time()
        emission = 60.0 / limit.per_minute
        tolerance = emission * max(1, limit.burst)
        ttl = int((tolerance + emission) * 1000) + 1000

        try:
            gcra_script, _ = self._scripts()
            allowed, retry, remaining = await gcra_script(
                keys=[bucket_key("key", key_id, cls)],
                args=[repr(now), repr(emission), repr(tolerance), str(ttl)],
            )
            if not int(allowed):
                return Decision(
                    False,
                    limit.burst,
                    0,
                    _ceil_seconds(float(retry)),
                    _ceil_seconds(float(retry)),
                    cls,
                    sustained=limit.per_minute,
                )

            ceiling = max(1, int(limit.per_minute * WORKSPACE_MULTIPLIER))
            w_emission = 60.0 / ceiling
            w_tolerance = w_emission * max(1, int(limit.burst * WORKSPACE_MULTIPLIER))
            w_allowed, w_retry, _w_remaining = await gcra_script(
                keys=[bucket_key("workspace", workspace_id, cls)],
                args=[
                    repr(now),
                    repr(w_emission),
                    repr(w_tolerance),
                    str(int((w_tolerance + w_emission) * 1000) + 1000),
                ],
            )
            if not int(w_allowed):
                return Decision(
                    False,
                    int(limit.burst * WORKSPACE_MULTIPLIER),
                    0,
                    _ceil_seconds(float(w_retry)),
                    _ceil_seconds(float(w_retry)),
                    cls,
                    sustained=ceiling,
                )

            return Decision(
                True,
                limit.burst,
                int(remaining),
                0,
                _ceil_seconds(tolerance),
                cls,
                sustained=limit.per_minute,
            )
        except Exception as exc:  # noqa: BLE001 - see the fail-open note above
            return Decision(
                True,
                limit.burst,
                0,
                0,
                0,
                cls,
                sustained=limit.per_minute,
                degraded=type(exc).__name__,
            )

    async def take_slot(self, *, key_id: str, cls: str, limit: Limit) -> str | None:
        """Claim one concurrency slot. Returns a token to release it with, or
        None when the caller already has as many in flight as they may."""
        if limit.concurrent <= 0 or not enabled():
            return ""
        token = uuid.uuid4().hex
        try:
            _, slot_script = self._scripts()
            taken, _held = await slot_script(
                keys=[slot_key(key_id, cls)],
                args=[
                    repr(time.time()),
                    str(SLOT_LEASE_SECONDS),
                    str(limit.concurrent),
                    token,
                ],
            )
            return token if int(taken) else None
        except Exception:  # noqa: BLE001 - fail open, as above
            return ""

    async def release_slot(self, *, key_id: str, cls: str, token: str) -> None:
        """Always called, including when the handler raised. A slot that is not
        released is capacity nobody can use until its lease expires."""
        if not token:
            return
        try:
            await self._redis.zrem(slot_key(key_id, cls), token)
        except Exception:  # noqa: BLE001 - a leaked slot expires on its own
            return


def _ceil_seconds(value: float) -> int:
    """Whole seconds, rounded up, and never zero — a Retry-After of 0 tells a
    client to retry immediately, which is what it was just refused for."""
    seconds = int(value)
    if value > seconds:
        seconds += 1
    return max(1, seconds)


__all__ = [
    "CLASSES",
    "SLOT_LEASE_SECONDS",
    "WORKSPACE_MULTIPLIER",
    "Decision",
    "Limit",
    "Limiter",
    "bucket_key",
    "default_limit",
    "enabled",
    "gcra",
    "limit_for",
    "slot_key",
]
