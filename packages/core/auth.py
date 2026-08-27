from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Identity: who is asking, and which workspace they are allowed to ask about.
#
# Two rules this module exists to enforce, both of which are easy to get subtly
# wrong and expensive to discover later:
#
#   1. A password is never stored, logged, or comparable in constant-unsafe
#      ways. bcrypt with a per-password salt; verification is the library's
#      constant-time compare.
#   2. A session token is never stored either — only its SHA-256. A dumped
#      database therefore cannot be replayed as a login, the same reasoning
#      that makes storing password hashes obvious.
#
# Roles are RECORDED, and today they gate exactly one thing: who may invite
# people. Everything else is deliberately not role-checked, because a role that
# claims to restrict something it doesn't is worse than no role at all — it
# tells an operator they are protected when they are not.

ROLES = ("owner", "manager", "member")
INVITE_ROLES = ("owner", "manager")  # who may bring someone else in

SESSION_TTL_DAYS = 30
INVITE_TTL_DAYS = 7
_MIN_PASSWORD = 10


class AuthError(Exception):
    """Anything the caller should turn into a 4xx without leaking detail."""


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------- passwords -------------------------------


def hash_password(password: str) -> str:
    if len(password) < _MIN_PASSWORD:
        raise AuthError(f"Password must be at least {_MIN_PASSWORD} characters.")
    # bcrypt silently truncates past 72 BYTES, so a long passphrase could
    # otherwise be weaker than it looks; reject rather than quietly clip it.
    if len(password.encode("utf-8")) > 72:
        raise AuthError("Password is too long (max 72 bytes).")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def normalize_email(email: str) -> str:
    return unicodedata.normalize("NFKC", email).strip().lower()


# --------------------------------- tokens ---------------------------------


def _new_token() -> tuple[str, str]:
    """(secret, hash). Only the hash is ever persisted."""
    secret = secrets.token_urlsafe(32)
    return secret, hashlib.sha256(secret.encode("utf-8")).hexdigest()


def token_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# -------------------------------- workspaces --------------------------------


def workspace_id(name: str) -> str:
    """A readable, collision-resistant id for a new workspace.

    `companies.id` is text and appears in URLs and logs, so a slug reads far
    better than a uuid; the random suffix keeps two teams called "Platform"
    from colliding.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", name).lower()).strip("-")
    return f"{slug[:32] or 'workspace'}-{secrets.token_hex(3)}"


# ---------------------------------- users ----------------------------------


async def create_user(
    session: AsyncSession, email: str, name: str, password: str
) -> dict[str, Any]:
    email = normalize_email(email)
    if not email or "@" not in email:
        raise AuthError("That doesn't look like an email address.")
    if not name.strip():
        raise AuthError("Please give a name.")
    digest = hash_password(password)
    row = (
        await session.execute(
            text(
                """
                INSERT INTO users (email, name, password_hash) VALUES (:e, :n, :p)
                ON CONFLICT (email) DO NOTHING
                RETURNING id, email, name, created_at
                """
            ),
            {"e": email, "n": name.strip(), "p": digest},
        )
    ).first()
    if row is None:
        raise AuthError("An account with that email already exists.")
    return {"id": row.id, "email": row.email, "name": row.name}


async def get_user_by_email(session: AsyncSession, email: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT id, email, name, password_hash FROM users WHERE email = :e"),
            {"e": normalize_email(email)},
        )
    ).first()
    if row is None:
        return None
    return {"id": row.id, "email": row.email, "name": row.name, "password_hash": row.password_hash}


async def authenticate(
    session: AsyncSession, email: str, password: str
) -> dict[str, Any]:
    """Verify credentials. The failure message is identical whether the email is
    unknown or the password is wrong — telling them apart hands an attacker a
    list of who has an account."""
    user = await get_user_by_email(session, email)
    if user is None:
        # still hash, so a missing account isn't detectably faster
        bcrypt.hashpw(b"timing", bcrypt.gensalt())
        raise AuthError("Email or password is incorrect.")
    if not verify_password(password, user["password_hash"]):
        raise AuthError("Email or password is incorrect.")
    await session.execute(
        text("UPDATE users SET last_login_at = now() WHERE id = :id"), {"id": user["id"]}
    )
    return {"id": user["id"], "email": user["email"], "name": user["name"]}


# ------------------------------- membership -------------------------------


async def add_membership(
    session: AsyncSession, user_id: int, company_id: str, role: str
) -> None:
    if role not in ROLES:
        raise AuthError(f"Unknown role {role!r}.")
    await session.execute(
        text(
            """
            INSERT INTO memberships (user_id, company_id, role) VALUES (:u, :c, :r)
            ON CONFLICT (user_id, company_id) DO UPDATE SET role = EXCLUDED.role
            """
        ),
        {"u": user_id, "c": company_id, "r": role},
    )


async def memberships(session: AsyncSession, user_id: int) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            """
            SELECT m.company_id, m.role, c.name
            FROM memberships m JOIN companies c ON c.id = m.company_id
            WHERE m.user_id = :u ORDER BY c.name
            """
        ),
        {"u": user_id},
    )
    return [{"company_id": r.company_id, "role": r.role, "name": r.name} for r in rows]


async def role_in(session: AsyncSession, user_id: int, company_id: str) -> str | None:
    return (
        await session.execute(
            text("SELECT role FROM memberships WHERE user_id = :u AND company_id = :c"),
            {"u": user_id, "c": company_id},
        )
    ).scalar_one_or_none()


async def create_workspace(
    session: AsyncSession, name: str, owner_id: int
) -> dict[str, Any]:
    company_id = workspace_id(name)
    await session.execute(
        text("INSERT INTO companies (id, name) VALUES (:id, :n)"),
        {"id": company_id, "n": name.strip() or company_id},
    )
    await add_membership(session, owner_id, company_id, "owner")
    # Nothing else is planted. A new workspace holds no documents and no
    # categories of its own — the platform taxonomy is already available to it,
    # and an empty library is the correct resting state, not a broken one.
    return {"company_id": company_id, "name": name.strip(), "role": "owner"}


# -------------------------------- sessions --------------------------------


async def start_session(
    session: AsyncSession, user_id: int, company_id: str
) -> tuple[str, datetime]:
    """Returns (cookie value, expiry). Only the hash is stored."""
    secret, digest = _new_token()
    expires_at = _now() + timedelta(days=SESSION_TTL_DAYS)
    await session.execute(
        text(
            """
            INSERT INTO auth_sessions (token_hash, user_id, company_id, expires_at)
            VALUES (:t, :u, :c, :e)
            """
        ),
        {"t": digest, "u": user_id, "c": company_id, "e": expires_at},
    )
    # starting a session IS signing in — registration issues one too, and a
    # brand-new member listed as "never signed in" is simply wrong
    await session.execute(
        text("UPDATE users SET last_login_at = now() WHERE id = :id"), {"id": user_id}
    )
    return secret, expires_at


async def resolve_session(session: AsyncSession, secret: str | None) -> dict[str, Any] | None:
    """The authenticated principal for a cookie, or None.

    Re-reads the membership on EVERY request rather than trusting what the
    session recorded at login: someone removed from a workspace must lose
    access immediately, not whenever their cookie happens to expire.
    """
    if not secret:
        return None
    row = (
        await session.execute(
            text(
                """
                SELECT s.id, s.user_id, s.company_id, u.email, u.name
                FROM auth_sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = :t
                  AND s.revoked_at IS NULL
                  AND s.expires_at > now()
                """
            ),
            {"t": token_hash(secret)},
        )
    ).first()
    if row is None:
        return None
    role = await role_in(session, row.user_id, row.company_id)
    if role is None:
        return None  # membership revoked since login
    await session.execute(
        text("UPDATE auth_sessions SET last_seen_at = now() WHERE id = :id"), {"id": row.id}
    )
    return {
        "session_id": row.id,
        "user_id": row.user_id,
        "email": row.email,
        "name": row.name,
        "company_id": row.company_id,
        "role": role,
    }


async def switch_workspace(session: AsyncSession, session_id: int, user_id: int, company_id: str) -> None:
    if await role_in(session, user_id, company_id) is None:
        raise AuthError("You are not a member of that workspace.")
    await session.execute(
        text("UPDATE auth_sessions SET company_id = :c WHERE id = :id AND user_id = :u"),
        {"c": company_id, "id": session_id, "u": user_id},
    )


async def revoke_session(session: AsyncSession, secret: str | None) -> None:
    if not secret:
        return
    await session.execute(
        text("UPDATE auth_sessions SET revoked_at = now() WHERE token_hash = :t"),
        {"t": token_hash(secret)},
    )


async def revoke_all_sessions(session: AsyncSession, user_id: int) -> None:
    """Used when a password changes — every other device must be logged out."""
    await session.execute(
        text("UPDATE auth_sessions SET revoked_at = now() WHERE user_id = :u AND revoked_at IS NULL"),
        {"u": user_id},
    )


# ------------------------------- invitations -------------------------------


async def create_invitation(
    session: AsyncSession, company_id: str, email: str, role: str, invited_by: int
) -> str:
    """Returns the single-use secret. Only its hash is stored, so an invite
    cannot be recovered from the database — it must be re-sent."""
    if role not in ROLES:
        raise AuthError(f"Unknown role {role!r}.")
    inviter = await role_in(session, invited_by, company_id)
    if inviter not in INVITE_ROLES:
        raise AuthError("Only an owner or manager can invite people.")
    email = normalize_email(email)
    secret, digest = _new_token()
    await session.execute(
        text("DELETE FROM invitations WHERE company_id = :c AND email = :e AND accepted_at IS NULL"),
        {"c": company_id, "e": email},
    )
    await session.execute(
        text(
            """
            INSERT INTO invitations (company_id, email, role, token_hash, invited_by, expires_at)
            VALUES (:c, :e, :r, :t, :b, now() + make_interval(days => :d))
            """
        ),
        {"c": company_id, "e": email, "r": role, "t": digest, "b": invited_by, "d": INVITE_TTL_DAYS},
    )
    return secret


async def accept_invitation(
    session: AsyncSession, secret: str, user_id: int
) -> dict[str, Any]:
    row = (
        await session.execute(
            text(
                """
                UPDATE invitations SET accepted_at = now()
                WHERE token_hash = :t AND accepted_at IS NULL AND expires_at > now()
                RETURNING company_id, role
                """
            ),
            {"t": token_hash(secret)},
        )
    ).first()
    if row is None:
        raise AuthError("That invitation is invalid, already used, or expired.")
    await add_membership(session, user_id, row.company_id, row.role)
    return {"company_id": row.company_id, "role": row.role}


async def list_members(session: AsyncSession, company_id: str) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            """
            SELECT u.id, u.name, u.email, m.role, u.last_login_at
            FROM memberships m JOIN users u ON u.id = m.user_id
            WHERE m.company_id = :c ORDER BY m.role, u.name
            """
        ),
        {"c": company_id},
    )
    return [
        {"id": r.id, "name": r.name, "email": r.email, "role": r.role,
         "last_login_at": r.last_login_at}
        for r in rows
    ]
