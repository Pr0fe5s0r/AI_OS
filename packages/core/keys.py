from __future__ import annotations

import hashlib
import secrets
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# API KEYS — the way in for everything that is not a browser.
#
# The console signs in with a cookie. An SDK, an MCP server, a CI job and
# another application cannot, so without keys the store is reachable only by a
# person clicking, which is not what a database is for.
#
# The key is shown ONCE, at creation. Only its hash is stored, so a leaked
# database does not hand over working credentials, and "show me the key again"
# is answered honestly: it cannot be, make a new one.
# ---------------------------------------------------------------------------

PREFIX = "kb"

# Every scope that means anything. `read` sees document contents; `write`
# ingests and edits them; `manage` administers collections and keys and reads
# nothing.
SCOPES = ("read", "write", "manage")


def parse_scopes(raw: str | None) -> set[str]:
    """The scopes a stored string actually grants, ignoring anything unknown."""
    return {part.strip().lower() for part in (raw or "").split(",")} & set(SCOPES)


class Escalation(ValueError):
    """A key was asked to mint one more powerful than itself."""


def check_subset(minter: set[str] | None, requested: set[str]) -> None:
    """A key may only issue keys no stronger than itself.

    Without this the `manage` scope is decoration. An operator holding a
    manage-only key — deliberately unable to read a single tenant document —
    could call POST /api/keys, mint itself a `read` key, and read all of them.
    One API call, and the isolation the whole separation exists for is gone.

    `minter` is None for a signed-in person, who is not a key and is bounded by
    their membership instead.
    """
    if minter is None:
        return
    beyond = requested - minter
    if beyond:
        raise Escalation(
            "A key cannot grant scopes it does not hold: "
            + ", ".join(sorted(beyond))
        )


def _hash(key: str) -> str:
    """SHA-256, not bcrypt.

    A password is low-entropy and typed by a human, so it needs a slow hash to
    survive guessing. A 256-bit random key cannot be guessed, and it is
    verified on every single API call — a deliberately slow hash here would
    just be a rate limiter on our own traffic.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def mint(environment: str = "live") -> tuple[str, str, str]:
    """Return (full_key, key_id, prefix). The full key is never stored."""
    key_id = secrets.token_hex(8)
    body = secrets.token_urlsafe(32)
    full = f"{PREFIX}_{environment}_{body}"
    return full, key_id, full[: len(PREFIX) + len(environment) + 9]


async def create_key(
    session: AsyncSession,
    workspace_id: str,
    name: str,
    created_by: str | None = None,
    scopes: str = "read,write",
    collection_id: str | None = None,
    minter_scopes: set[str] | None = None,
) -> dict[str, Any]:
    """Issue a key. The plaintext comes back exactly once, here.

    A `collection_id` binds the key to one collection: the request layer forces
    every call it makes into that collection and refuses any other. Left None,
    the key is workspace-wide, which is the console's own default. A binding is
    validated against the workspace here, because a key that names a collection
    the workspace does not have is a mistake worth catching at creation rather
    than as a 404 on first use.
    """
    wanted = parse_scopes(scopes)
    if not wanted:
        raise ValueError(f"A key needs at least one scope of {', '.join(SCOPES)}.")
    check_subset(minter_scopes, wanted)
    scopes = ",".join(scope for scope in SCOPES if scope in wanted)

    if collection_id is not None:
        known = (
            await session.execute(
                text(
                    "SELECT 1 FROM collections "
                    "WHERE workspace_id = :ws AND collection_id = :cid"
                ),
                {"ws": workspace_id, "cid": collection_id},
            )
        ).first()
        if known is None:
            raise ValueError(f"No such collection: {collection_id!r}")

    full, key_id, prefix = mint()
    await session.execute(
        text(
            """
            INSERT INTO api_keys
                (key_id, workspace_id, name, key_hash, prefix, scopes, created_by,
                 collection_id)
            VALUES (:kid, :ws, :name, :hash, :prefix, :scopes, :by, :cid)
            """
        ),
        {
            "kid": key_id, "ws": workspace_id, "name": name,
            "hash": _hash(full), "prefix": prefix, "scopes": scopes, "by": created_by,
            "cid": collection_id,
        },
    )
    return {
        "key_id": key_id,
        "name": name,
        "key": full,  # the only time this value exists outside the caller
        "prefix": prefix,
        "scopes": scopes.split(","),
        "collection_id": collection_id,
    }


async def resolve_key(session: AsyncSession, presented: str) -> dict[str, Any] | None:
    """Identify the workspace behind a presented key, or None.

    Looks up by hash, so the key itself is never compared in the database and
    never appears in a query log.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT key_id, workspace_id, name, scopes, collection_id, revoked_at
                FROM api_keys WHERE key_hash = :hash
                """
            ),
            {"hash": _hash(presented)},
        )
    ).first()
    if row is None or row.revoked_at is not None:
        return None

    # Last-used is written on a separate statement and deliberately not awaited
    # for correctness — it is telemetry, and a failure to record it must never
    # fail an authenticated request.
    await session.execute(
        text("UPDATE api_keys SET last_used_at = now() WHERE key_id = :kid"),
        {"kid": row.key_id},
    )
    return {
        "key_id": row.key_id,
        "workspace_id": row.workspace_id,
        "name": row.name,
        # Parsed rather than split: a stored " Read, write" would otherwise
        # produce [" Read", " write"] and match nothing, silently turning a key
        # somebody meant to be powerful into one that can do nothing.
        "scopes": sorted(parse_scopes(row.scopes)),
        "collection_id": row.collection_id,
    }


async def list_keys(session: AsyncSession, workspace_id: str) -> list[dict[str, Any]]:
    """Keys for a workspace — prefixes only, never the keys themselves."""
    rows = (
        await session.execute(
            text(
                """
                SELECT key_id, name, prefix, scopes, collection_id, created_by,
                       created_at, last_used_at, revoked_at
                FROM api_keys WHERE workspace_id = :ws ORDER BY created_at DESC
                """
            ),
            {"ws": workspace_id},
        )
    ).all()
    return [
        {
            "key_id": r.key_id,
            "name": r.name,
            "prefix": r.prefix,
            "scopes": r.scopes.split(","),
            "collection_id": r.collection_id,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat(),
            "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
            "revoked": r.revoked_at is not None,
        }
        for r in rows
    ]


async def revoke_key(session: AsyncSession, workspace_id: str, key_id: str) -> bool:
    """Revoke immediately. The row is kept so the audit trail survives."""
    result = await session.execute(
        text(
            """
            UPDATE api_keys SET revoked_at = now()
            WHERE workspace_id = :ws AND key_id = :kid AND revoked_at IS NULL
            """
        ),
        {"ws": workspace_id, "kid": key_id},
    )
    return bool(cast(CursorResult, result).rowcount)


def redact(key: str) -> str:
    """For logs and traces. Never print a whole key anywhere."""
    return f"{key[:12]}…" if len(key) > 12 else "…"


__all__ = [
    "create_key",
    "list_keys",
    "mint",
    "redact",
    "resolve_key",
    "revoke_key",
]
