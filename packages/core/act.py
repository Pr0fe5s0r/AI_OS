from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core import audit
from packages.core.credentials import get_credential
from packages.shared.schema import ActionRequest, ActionResult

# Generic action runtime with human-approval gating.
#
# action_registry (vertical data):
#   "apply_label":   {"kind": "http", "approval_required": False, ...,
#                     "auth": {"source": "github"}}
#   "page_engineer": {"kind": "log",  "approval_required": True, "template": "..."}
# approval_policy (vertical data):
#   {"default_require_approval": True, "dry_run": True, "force_approval": <bool>}
#
# The agent may execute low-risk actions on its own. Two things pull a human in:
#   1. the action itself is risky      -> registry `approval_required: True`
#   2. the caller says so this time    -> policy `force_approval: True`
#      (the vertical sets this from severity / model confidence)
#
# SAFETY: dry_run defaults to True. Nothing is sent to an external system until
# the operator explicitly disables it. A dry run records exactly what WOULD be
# sent — minus credentials, which are never persisted.


def needs_approval(entry: dict, approval_policy: dict) -> bool:
    """Pure gate: does this action require a human right now?"""
    # pre_approved: a human explicitly requested THIS action (e.g. clicked a
    # button in the UI). Asking them to approve their own click again adds a
    # step without adding safety — dry_run still applies downstream.
    if approval_policy.get("pre_approved", False):
        return False
    if entry.get("approval_required", approval_policy.get("default_require_approval", True)):
        return True
    return bool(approval_policy.get("force_approval", False))


async def _insert(session: AsyncSession, req: ActionRequest, status: str, detail: str, result: dict) -> int:
    row = await session.execute(
        text(
            """
            INSERT INTO actions
                (company_id, situation_id, action, params, status, result, detail, requested_by)
            VALUES (:c, :sid, :a, CAST(:p AS jsonb), :st, CAST(:r AS jsonb), :d, :by)
            RETURNING id
            """
        ),
        {
            "c": req.company_id,
            "sid": req.situation_id,
            "a": req.action,
            "p": json.dumps(req.params),
            "st": status,
            "r": json.dumps(result),
            "d": detail,
            "by": req.requested_by,
        },
    )
    return int(row.scalar_one())


async def _auth_headers(session: AsyncSession | None, entry: dict, company_id: str) -> dict[str, str]:
    """Fetch the sealed token for this action's source at call time. Never stored."""
    auth = entry.get("auth")
    if not auth or session is None:
        return {}
    cred = await get_credential(session, company_id, auth["source"])
    token = cred[0] if cred else ""
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _execute(
    entry: dict,
    req: ActionRequest,
    approval_policy: dict,
    session: AsyncSession | None = None,
) -> tuple[str, str, dict]:
    """Run a registered action. Returns (status, detail, result)."""
    kind = entry.get("kind", "log")
    dry_run = bool(approval_policy.get("dry_run", True))

    if kind == "log":
        try:
            msg = str(entry.get("template", req.action)).format(**{**req.params, "action": req.action})
        except KeyError as missing:
            return "failed", f"missing parameter {missing} for {req.action!r}", {}
        return "executed", msg, {"logged": msg}

    if kind == "http":
        try:
            url = str(entry.get("url", "")).format(**req.params)
        except KeyError as missing:
            # a caller forgot e.g. `repo` — record a failed action, never 500
            return "failed", f"missing parameter {missing} for {req.action!r}", {}

        body = req.params.get("body", entry.get("body", {}))
        method = entry.get("method", "POST")
        # what we'd send, with credentials deliberately omitted
        preview = {"method": method, "url": url, "body": body}

        if dry_run:
            return "dry_run", "dry_run: request not sent", {"would_send": preview}

        headers = await _auth_headers(session, entry, req.company_id)
        if entry.get("auth") and not headers:
            return "failed", f"no credential stored for {entry['auth']['source']}", {"would_send": preview}

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.request(method, url, json=body, headers=headers)
        except httpx.HTTPError as exc:  # DNS, timeout, connection reset
            return "failed", f"request failed: {exc}", {"would_send": preview}

        return (
            "executed" if resp.is_success else "failed",
            f"HTTP {resp.status_code}",
            {"sent": preview, "status_code": resp.status_code, "body": resp.text[:500]},
        )

    return "failed", f"unknown action kind: {kind!r}", {}


async def act(
    state: dict,
    action: ActionRequest,
    action_registry: dict,
    approval_policy: dict,
) -> ActionResult:
    """Execute an action, pausing for human approval when required.

    state = {"session": AsyncSession}
    """
    session: AsyncSession = state["session"]
    entry = action_registry.get(action.action)
    if entry is None:
        return ActionResult(action=action.action, status="failed", detail="action not registered")

    if needs_approval(entry, approval_policy):
        action_id = await _insert(session, action, "pending_approval", "awaiting human approval", {})
        await audit.record(
            session, action.company_id, action.requested_by, "action.requested",
            target=action.action, metadata={"action_id": action_id, "situation_id": action.situation_id},
        )
        return ActionResult(
            id=action_id, action=action.action, status="pending_approval",
            detail="awaiting human approval",
        )

    status, detail, result = await _execute(entry, action, approval_policy, session)
    action_id = await _insert(session, action, status, detail, result)
    await audit.record(
        session, action.company_id, action.requested_by, f"action.{status}",
        target=action.action, metadata={"action_id": action_id},
    )
    return ActionResult(id=action_id, action=action.action, status=status, detail=detail, result=result)


async def decide(
    session: AsyncSession,
    action_id: int,
    approve: bool,
    decided_by: str,
    action_registry: dict,
    approval_policy: dict,
) -> ActionResult:
    """Approve (and run) or reject a pending action."""
    row = (
        await session.execute(
            text(
                """
                SELECT id, company_id, situation_id, action, params, status
                FROM actions WHERE id = :i
                """
            ),
            {"i": action_id},
        )
    ).first()
    if row is None:
        return ActionResult(action="?", status="failed", detail="action not found")
    if row.status != "pending_approval":
        return ActionResult(id=row.id, action=row.action, status=row.status, detail="already decided")

    params = row.params if isinstance(row.params, dict) else json.loads(row.params)
    req = ActionRequest(
        company_id=row.company_id, action=row.action, params=params,
        situation_id=row.situation_id, requested_by=decided_by,
    )

    if not approve:
        await session.execute(
            text("UPDATE actions SET status='rejected', decided_by=:b, decided_at=:t, detail='rejected by human' WHERE id=:i"),
            {"i": action_id, "b": decided_by, "t": datetime.now(UTC)},
        )
        await audit.record(session, row.company_id, decided_by, "action.rejected", target=row.action,
                           metadata={"action_id": action_id})
        return ActionResult(id=action_id, action=row.action, status="rejected", detail="rejected by human")

    entry = action_registry.get(row.action, {})
    status, detail, result = await _execute(entry, req, approval_policy, session)
    await session.execute(
        text(
            """
            UPDATE actions SET status=:st, detail=:d, result=CAST(:r AS jsonb),
                   decided_by=:b, decided_at=:t
            WHERE id=:i
            """
        ),
        {"i": action_id, "st": status, "d": detail, "r": json.dumps(result), "b": decided_by, "t": datetime.now(UTC)},
    )
    await audit.record(session, row.company_id, decided_by, f"action.approved.{status}", target=row.action,
                       metadata={"action_id": action_id})
    return ActionResult(id=action_id, action=row.action, status=status, detail=detail, result=result)


async def list_actions(session: AsyncSession, company_id: str, limit: int = 50) -> list[dict]:
    rows = await session.execute(
        text(
            """
            SELECT id, situation_id, action, params, status, detail, result,
                   requested_by, decided_by, requested_at, decided_at
            FROM actions WHERE company_id = :c ORDER BY requested_at DESC LIMIT :l
            """
        ),
        {"c": company_id, "l": limit},
    )
    out = []
    for r in rows:
        out.append(
            {
                "id": r.id,
                "situation_id": r.situation_id,
                "action": r.action,
                "params": r.params if isinstance(r.params, dict) else json.loads(r.params),
                "status": r.status,
                "detail": r.detail,
                "result": r.result if isinstance(r.result, dict) else json.loads(r.result),
                "requested_by": r.requested_by,
                "decided_by": r.decided_by,
                "requested_at": r.requested_at.isoformat(),
                "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            }
        )
    return out
