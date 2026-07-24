from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Cookie, Depends, HTTPException, Response

from packages.core import audit, auth
from packages.core.db import Session
from packages.core.tenancy import SESSION_COOKIE, company_scope, current_principal

# Registration, sign-in, workspaces and invitations.
#
# These are the ONLY routes that do not require a session. Everything else in
# the API depends on company_scope, which 401s without one — so a new endpoint
# is secure by default rather than by remembering to add a check.

router = APIRouter()

# Cookies are marked Secure in production; over plain http on localhost a Secure
# cookie is silently dropped by the browser, which looks exactly like "login is
# broken". Driven by env so the dev default can never ship as the prod one.
SESSION_COOKIE_SECURE = False


def _set_session_cookie(response: Response, secret: str, expires_at: datetime) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        secret,
        httponly=True,   # unreadable from JavaScript, so XSS cannot steal it
        samesite="lax",  # not sent on cross-site POSTs
        secure=SESSION_COOKIE_SECURE,
        path="/",
        expires=expires_at,
    )


def _payload(principal: dict[str, Any], member_list: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "user": {
            "id": principal["user_id"],
            "name": principal["name"],
            "email": principal["email"],
        },
        "workspace": {"company_id": principal["company_id"], "role": principal["role"]},
        "workspaces": member_list,
    }


@router.post("/api/auth/register")
async def register(response: Response, payload: dict = Body(...)) -> dict:
    """Create an account, then either start a workspace or join one by invite.

    The password is hashed inside core.auth; the plaintext is never stored,
    echoed back, or written to a log.
    """
    async with Session() as session:
        try:
            user = await auth.create_user(
                session,
                email=str(payload.get("email", "")),
                name=str(payload.get("name", "")),
                password=str(payload.get("password", "")),
            )
            invite = str(payload.get("invite_token") or "").strip()
            if invite:
                company_id = (await auth.accept_invitation(session, invite, user["id"]))["company_id"]
            else:
                workspace_name = str(payload.get("workspace_name") or "").strip()
                if not workspace_name:
                    raise auth.AuthError("Give your workspace a name.")
                company_id = (
                    await auth.create_workspace(session, workspace_name, user["id"])
                )["company_id"]
            secret, expires_at = await auth.start_session(session, user["id"], company_id)
            member_list = await auth.memberships(session, user["id"])
            await session.commit()
        except auth.AuthError as exc:
            raise HTTPException(400, str(exc)) from exc

    _set_session_cookie(response, secret, expires_at)
    role = next((m["role"] for m in member_list if m["company_id"] == company_id), "member")
    return _payload({**user, "user_id": user["id"], "company_id": company_id, "role": role}, member_list)


@router.post("/api/auth/login")
async def login(response: Response, payload: dict = Body(...)) -> dict:
    async with Session() as session:
        try:
            user = await auth.authenticate(
                session, str(payload.get("email", "")), str(payload.get("password", ""))
            )
        except auth.AuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        member_list = await auth.memberships(session, user["id"])
        if not member_list:
            raise HTTPException(403, "Your account does not belong to a workspace yet.")
        company_id = member_list[0]["company_id"]
        secret, expires_at = await auth.start_session(session, user["id"], company_id)
        await session.commit()

    _set_session_cookie(response, secret, expires_at)
    return _payload(
        {**user, "user_id": user["id"], "company_id": company_id, "role": member_list[0]["role"]},
        member_list,
    )


@router.post("/api/auth/logout")
async def logout(response: Response, markos_session: str | None = Cookie(default=None)) -> dict:
    """Revoked server-side, so the session is dead even if the cookie survives."""
    async with Session() as session:
        await auth.revoke_session(session, markos_session)
        await session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/api/auth/me")
async def me(principal: dict = Depends(current_principal)) -> dict:
    async with Session() as session:
        member_list = await auth.memberships(session, principal["user_id"])
    return _payload(principal, member_list)


@router.post("/api/auth/workspaces")
async def create_workspace(
    payload: dict = Body(...), principal: dict = Depends(current_principal)
) -> dict:
    """One person, several workspaces — a team per project, or per client."""
    async with Session() as session:
        try:
            created = await auth.create_workspace(
                session, str(payload.get("name", "")), principal["user_id"]
            )
            await auth.switch_workspace(
                session, principal["session_id"], principal["user_id"], created["company_id"]
            )
            await session.commit()
        except auth.AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
    return created


@router.post("/api/auth/switch")
async def switch(payload: dict = Body(...), principal: dict = Depends(current_principal)) -> dict:
    async with Session() as session:
        try:
            await auth.switch_workspace(
                session, principal["session_id"], principal["user_id"],
                str(payload.get("company_id", "")),
            )
            await session.commit()
        except auth.AuthError as exc:
            raise HTTPException(403, str(exc)) from exc
    return {"company_id": payload.get("company_id")}


@router.get("/api/team")
async def team(
    company_id: str = Depends(company_scope), principal: dict = Depends(current_principal)
) -> dict:
    async with Session() as session:
        members = await auth.list_members(session, company_id)
    return {
        "members": members,
        "your_role": principal["role"],
        "can_invite": principal["role"] in auth.INVITE_ROLES,
    }


@router.post("/api/team/invite")
async def invite(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
    principal: dict = Depends(current_principal),
) -> dict:
    """Returns the invite secret ONCE. Only its hash is stored, so it can never
    be recovered from the database — a lost invite is re-issued, not looked up."""
    async with Session() as session:
        try:
            secret = await auth.create_invitation(
                session, company_id, str(payload.get("email", "")),
                str(payload.get("role", "member")), principal["user_id"],
            )
        except auth.AuthError as exc:
            raise HTTPException(403, str(exc)) from exc
        await audit.record(
            session, company_id, principal["email"], "team.invited",
            target=str(payload.get("email", "")),
            metadata={"role": payload.get("role", "member")},
        )
        await session.commit()
    return {"invite_token": secret, "expires_in_days": auth.INVITE_TTL_DAYS}


@router.post("/api/team/accept")
async def accept(payload: dict = Body(...), principal: dict = Depends(current_principal)) -> dict:
    """An existing user joining another workspace they were invited to."""
    async with Session() as session:
        try:
            joined = await auth.accept_invitation(
                session, str(payload.get("invite_token", "")), principal["user_id"]
            )
        except auth.AuthError as exc:
            raise HTTPException(400, str(exc)) from exc
        await session.commit()
    return joined
