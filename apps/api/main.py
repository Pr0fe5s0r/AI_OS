from __future__ import annotations

import json
import os

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from packages.core import audit
from packages.core.act import decide, list_actions
from packages.core.briefing import build_briefing
from packages.core.credentials import delete_credential, list_connections, save_credential
from packages.core.db import Session
from packages.core.graph import query_graph
from packages.core.norms import get_norms
from packages.core.search import search
from packages.core.settings import set_setting
from packages.core.situations import get_situation, list_situations
from packages.core.tenancy import company_scope
from packages.core.tickets import close_ticket, get_ticket, list_tickets
from packages.core.tokens import consume_token, peek_token
from packages.core.webhooks import verify_signature
from packages.core.workload import open_workload
from packages.shared.schema import TeamMember
from verticals.software.alerting import (
    ASSIGN_PURPOSE,
    accept_assignment,
    complete_ticket,
    free_engineers,
    propose_assignment,
    request_action,
    run_analysis,
)
from verticals.software.config import (
    ACTION_REGISTRY,
    ASSIGNABLE_ROLE,
    AUTONOMY_POLICY,
    BRIEFING_POLICY,
    COMPANY_ID,
    DRY_RUN_KEY,
    GRAPH_SCHEMA,
    NOTIFY_ROLES,
    TEAM_ROLES,
    WORKLOAD_SPEC,
    approval_policy,
    save_team_roster,
    team_roster,
)
from verticals.software.ingest import trigger_ingest
from verticals.software.webhooks import (
    EVENT_HEADER,
    SIGNATURE_HEADER,
    handle_github_webhook,
)

app = FastAPI(title="AI OS")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

SUPPORTED = ("github", "slack", "zendesk")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ------------------------------ Webhooks ------------------------------


@app.post("/api/webhooks/github")
async def github_webhook(request: Request) -> dict:
    """Event-driven entry: GitHub pushes issue/PR changes the moment they happen.

    The body must be signed with GITHUB_WEBHOOK_SECRET — this URL is public, and
    an unsigned payload could make the AI email people and write to the repo.
    """
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(
            503, "GITHUB_WEBHOOK_SECRET is not set; refusing unauthenticated webhooks"
        )
    body = await request.body()
    if not verify_signature(secret, body, request.headers.get(SIGNATURE_HEADER)):
        raise HTTPException(401, "invalid webhook signature")

    event_type = request.headers.get(EVENT_HEADER, "")
    if event_type == "ping":  # GitHub's handshake when the webhook is created
        return {"ok": True, "pong": True}

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(400, "body is not JSON") from exc

    async with Session() as session:
        result = await handle_github_webhook(session, event_type, payload)
        await audit.record(
            session, COMPANY_ID, "github-webhook", "webhook.received",
            target=event_type, metadata=result,
        )
        await session.commit()
    return {"ok": True, **result}


# ---------------------------- Briefing -----------------------------


@app.get("/api/briefing")
async def briefing(company_id: str = Depends(company_scope)) -> dict:
    """Workspace briefing: generic state summary + the vertical's briefing policy."""
    async with Session() as session:
        return await build_briefing(session, company_id, BRIEFING_POLICY)


# --------------------- One-click email actions + tickets ---------------------


def _page(title: str, body: str, tone: str = "#1f883d") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>{title}</title>
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:560px;
            margin:64px auto;padding:24px;border:1px solid #d0d7de;border-radius:10px">
  <h2 style="margin:0 0 8px;color:{tone}">{title}</h2>
  {body}
</div>""")


@app.get("/api/act/{token}", response_class=HTMLResponse)
async def confirm_email_action(token: str) -> HTMLResponse:
    """Rendered when the PM clicks the link. Does NOT consume the token — mail
    scanners pre-fetch links, and a GET must never change anything."""
    async with Session() as session:
        payload = await peek_token(session, token, ASSIGN_PURPOSE)
    if payload is None:
        return _page("Link expired", "<p>This link is invalid, already used, or expired.</p>", "#d1242f")
    return _page(
        "Confirm assignment",
        f"""<p>Assign <b>{payload['repo']}#{payload['number']}</b> to
            <b>{payload['assignee']}</b>?</p>
        <p style="color:#57606a;font-size:13px">{payload.get('title','')}</p>
        <form method="post" action="/api/act/{token}">
          <button type="submit" style="padding:10px 18px;border:0;border-radius:6px;
            background:#1f883d;color:#fff;font-weight:600;font-size:14px;cursor:pointer">
            Yes, assign it
          </button>
        </form>""",
    )


@app.post("/api/act/{token}", response_class=HTMLResponse)
async def perform_email_action(token: str) -> HTMLResponse:
    """Burns the single-use token, assigns on GitHub, and opens a ticket."""
    async with Session() as session:
        payload = await consume_token(session, token, ASSIGN_PURPOSE, used_by="email-link")
        if payload is None:
            await session.commit()
            return _page("Link expired", "<p>This link is invalid, already used, or expired.</p>", "#d1242f")
        company_id = payload["company_id"]
        result = await accept_assignment(session, payload, company_id, clicked_by="project_manager")
        await audit.record(
            session, company_id, "project_manager", "assignment.accepted",
            target=payload["assignee"], metadata={"ticket_id": result["ticket_id"],
                                                  "situation_id": payload["situation_id"]},
        )
        await session.commit()

    note = "" if not result["dry_run"] else "<p style='color:#bf8700'>Practice mode: GitHub was not updated.</p>"
    return _page(
        "Assigned",
        f"""<p><b>{result['assignee']}</b> now owns
            <b>{payload['repo']}#{payload['number']}</b>.</p>
        <p>Ticket <b>#{result['ticket_id']}</b> was created for them.</p>
        <p style="color:#57606a;font-size:13px">GitHub: {result['assign_status']} - {result['detail']}</p>
        {note}""",
    )


@app.get("/api/tickets")
async def tickets(
    company_id: str = Depends(company_scope),
    assignee: str | None = None,
) -> dict:
    async with Session() as session:
        items = await list_tickets(session, company_id, assignee)
    return {"count": len(items), "tickets": [t.model_dump(mode="json") for t in items]}


@app.post("/api/tickets/{ticket_id}/close")
async def finish_ticket(
    ticket_id: int,
    payload: dict = Body(default={}),
    company_id: str = Depends(company_scope),
) -> dict:
    """The assignee finished. Close the ticket, then close the GitHub issue."""
    by = str(payload.get("by") or "assignee")
    async with Session() as session:
        ticket = await get_ticket(session, company_id, ticket_id)
        if ticket is None:
            raise HTTPException(404, f"no ticket {ticket_id}")
        if ticket.status == "done":
            raise HTTPException(400, "ticket already closed")

        closed = await close_ticket(session, company_id, ticket_id)
        github = await complete_ticket(session, ticket, company_id, by)
        await audit.record(session, company_id, by, "ticket.closed", target=str(ticket_id),
                           metadata={"github": github["github"]})
        await session.commit()
    return {"ticket": closed.model_dump(mode="json") if closed else None, "github": github}


# ------------------------------- Team --------------------------------


@app.get("/api/team")
async def get_team(company_id: str = Depends(company_scope)) -> dict:
    """The roster, annotated with each person's live workload and availability."""
    async with Session() as session:
        roster = await team_roster(session, company_id)
        workload = await open_workload(session, company_id, WORKLOAD_SPEC)
    free = {m["id"] for m in free_engineers(roster, workload)}
    members = [
        {
            **m,
            "open_issues": workload.get(m["id"], 0),
            "free": m["id"] in free,
        }
        for m in roster
    ]
    return {
        "members": members,
        "roles": TEAM_ROLES,
        "notify_roles": NOTIFY_ROLES,
        "assignable_role": ASSIGNABLE_ROLE,
    }


def _validate_member(payload: dict) -> dict:
    try:
        member = TeamMember(**payload)
    except Exception as exc:
        raise HTTPException(400, f"invalid member: {exc}") from exc
    if "@" not in member.email:
        raise HTTPException(400, "email must be a real address")
    unknown = [r for r in member.roles if r not in TEAM_ROLES]
    if unknown:
        raise HTTPException(400, f"unknown role(s): {unknown}. allowed: {TEAM_ROLES}")
    if not member.roles:
        raise HTTPException(400, f"give the member at least one role: {TEAM_ROLES}")
    if member.max_open_issues < 1:
        raise HTTPException(400, "max_open_issues must be at least 1")
    return member.model_dump()


@app.post("/api/team")
async def upsert_member(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
) -> dict:
    member = _validate_member(payload)
    async with Session() as session:
        roster = await team_roster(session, company_id)
        roster = [m for m in roster if m["id"] != member["id"]] + [member]
        await save_team_roster(session, roster, company_id)
        await audit.record(session, company_id, "ui", "team.member.saved",
                           target=member["id"], metadata={"roles": member["roles"]})
        await session.commit()
    return {"member": member, "members": roster}


@app.delete("/api/team/{member_id}")
async def remove_member(member_id: str, company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        roster = await team_roster(session, company_id)
        if not any(m["id"] == member_id for m in roster):
            raise HTTPException(404, f"no team member {member_id!r}")
        roster = [m for m in roster if m["id"] != member_id]
        await save_team_roster(session, roster, company_id)
        await audit.record(session, company_id, "ui", "team.member.removed", target=member_id)
        await session.commit()
    return {"members": roster}


# ----------------------------- Connect -----------------------------


@app.get("/api/connections")
async def get_connections(company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        conns = await list_connections(session, company_id)
    connected = {c.source: c for c in conns}
    return {
        "connections": [
            {
                "source": s,
                "connected": s in connected,
                "config": connected[s].config if s in connected else {},
            }
            for s in SUPPORTED
        ]
    }


@app.post("/api/connections/{source}")
async def connect(
    source: str,
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
) -> dict:
    if source not in SUPPORTED:
        raise HTTPException(400, f"unsupported source: {source}")
    token = payload.pop("token", "") or ""
    async with Session() as session:
        await save_credential(session, company_id, source, token, payload)
        await audit.record(session, company_id, "api", "connection.saved", target=source,
                           metadata={"config": payload})
        await session.commit()
    return {"source": source, "connected": True, "config": payload}


@app.delete("/api/connections/{source}")
async def disconnect(source: str, company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        await delete_credential(session, company_id, source)
        await audit.record(session, company_id, "api", "connection.removed", target=source)
        await session.commit()
    return {"source": source, "connected": False}


@app.post("/api/connections/{source}/sync")
async def sync(source: str, company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        try:
            counts = await trigger_ingest(session, company_id, only_source=source)
        except Exception as exc:  # surface connector errors to the UI
            raise HTTPException(502, f"{source} sync failed: {exc}") from exc
        await audit.record(session, company_id, "api", "ingest.trigger", target=source,
                           metadata=counts)
        await session.commit()
    return {"enqueued": counts}


@app.post("/api/ingest")
async def ingest_all(company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        counts = await trigger_ingest(session, company_id)
        await audit.record(session, company_id, "api", "ingest.trigger", metadata=counts)
        await session.commit()
    return {"enqueued": counts}


# ------------------------------ Events ------------------------------


@app.get("/api/events")
async def events(
    company_id: str = Depends(company_scope),
    source: str | None = None,
    type: str | None = None,
    state: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    clauses = ["company_id = :c"]
    params: dict = {"c": company_id, "l": limit}
    if source:
        clauses.append("source = :s")
        params["s"] = source
    if type:
        clauses.append("type = :t")
        params["t"] = type
    if state:
        clauses.append("metadata->>'state' = :st")
        params["st"] = state

    async with Session() as session:
        rows = await session.execute(
            text(
                f"""
                SELECT id, source, type, actor_name, timestamp, content, metadata
                FROM events WHERE {" AND ".join(clauses)}
                ORDER BY timestamp DESC LIMIT :l
                """
            ),
            params,
        )
        out = []
        for r in rows:
            md = r.metadata if isinstance(r.metadata, dict) else json.loads(r.metadata)
            out.append(
                {
                    "id": r.id,
                    "source": r.source,
                    "type": r.type,
                    "actor": {"id": r.actor_name, "name": r.actor_name},
                    "timestamp": r.timestamp.isoformat(),
                    "content": r.content,
                    "metadata": md or {},
                }
            )
    return {"count": len(out), "events": out}


# ------------------------------ Search ------------------------------


@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1),
    company_id: str = Depends(company_scope),
) -> dict:
    async with Session() as session:
        results = await search(session, company_id, q)
    return {"query": q, "company_id": company_id, "count": len(results), "results": results}


# ---------------------------- Understand ----------------------------


@app.get("/api/entity/{event_id}/related")
async def related(
    event_id: str,
    company_id: str = Depends(company_scope),
    hops: int = Query(2, ge=1, le=4),
) -> dict:
    async with Session() as session:
        result = await query_graph(session, company_id, event_id, GRAPH_SCHEMA, hops)
    return result.model_dump()


@app.get("/api/norms")
async def norms(company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        baselines = await get_norms(session, company_id)
    return {"company_id": company_id, "norms": [b.model_dump() for b in baselines]}


# ------------------------------ Alert -------------------------------


@app.post("/api/analyze")
async def analyze(company_id: str = Depends(company_scope)) -> dict:
    """Learn norms -> detect situations -> assemble briefs -> deliver."""
    async with Session() as session:
        summary = await run_analysis(session, company_id)
        await audit.record(session, company_id, "api", "analyze.run", metadata=summary)
        await session.commit()
    return summary


@app.get("/api/situations")
async def situations(company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        items = await list_situations(session, company_id)
    return {"count": len(items), "situations": [s.model_dump(mode="json") for s in items]}


@app.post("/api/situations/{situation_id}/propose")
async def propose(situation_id: str, company_id: str = Depends(company_scope)) -> dict:
    """The Flags screen's Assign button: same candidates, same AI pick, same
    single-use links as the email brief."""
    async with Session() as session:
        situation = await get_situation(session, company_id, situation_id)
        if situation is None:
            raise HTTPException(404, "situation not found")
        roster = await team_roster(session, company_id)
        proposal = await propose_assignment(session, situation, company_id, roster)
        await session.commit()  # the minted tokens must survive this request
    return {
        **{k: v for k, v in proposal.items() if k != "links"},
        "links": [link.model_dump() for link in proposal.get("links", [])],
    }


# ------------------------------- Act --------------------------------


@app.get("/api/actions")
async def actions(company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        items = await list_actions(session, company_id)
        policy = await approval_policy(session, company_id)
    return {"count": len(items), "actions": items, "dry_run": policy["dry_run"]}


@app.get("/api/actions/registry")
async def action_registry(company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        policy = await approval_policy(session, company_id)
    return {
        "actions": [
            {"name": k, "approval_required": v.get("approval_required", True), "kind": v.get("kind")}
            for k, v in ACTION_REGISTRY.items()
        ],
        "policy": policy,
        "autonomy": AUTONOMY_POLICY,
    }


@app.get("/api/settings")
async def get_settings(company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        policy = await approval_policy(session, company_id)
    return {"dry_run": policy["dry_run"], "autonomy": AUTONOMY_POLICY}


@app.post("/api/settings/dry_run")
async def set_dry_run(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
) -> dict:
    """Toggle practice mode. Persisted per company, so it holds while nobody is watching."""
    if "enabled" not in payload:
        raise HTTPException(400, "body must be {\"enabled\": true|false}")
    enabled = bool(payload["enabled"])
    async with Session() as session:
        await set_setting(session, company_id, DRY_RUN_KEY, enabled)
        await audit.record(
            session, company_id, "ui", "settings.dry_run",
            target="practice_mode" if enabled else "live_writes",
            metadata={"dry_run": enabled},
        )
        await session.commit()
    return {"dry_run": enabled}


@app.post("/api/actions")
async def create_action(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
) -> dict:
    action = str(payload.get("action", ""))
    if action not in ACTION_REGISTRY:
        raise HTTPException(400, f"unknown action: {action}")
    async with Session() as session:
        result = await request_action(
            session,
            action=action,
            params=payload.get("params", {}),
            situation_id=payload.get("situation_id"),
            company_id=company_id,
            requested_by=payload.get("requested_by", "ui"),
        )
    return result.model_dump()


@app.post("/api/actions/{action_id}/{verdict}")
async def decide_action(
    action_id: int,
    verdict: str,
    company_id: str = Depends(company_scope),
) -> dict:
    if verdict not in ("approve", "reject"):
        raise HTTPException(400, "verdict must be approve or reject")
    async with Session() as session:
        policy = await approval_policy(session, company_id)
        result = await decide(
            session, action_id, verdict == "approve", "ui", ACTION_REGISTRY, policy
        )
        await session.commit()
    return result.model_dump()


@app.get("/api/audit")
async def audit_log(company_id: str = Depends(company_scope), limit: int = 50) -> dict:
    async with Session() as session:
        rows = await session.execute(
            text(
                """
                SELECT actor, action, target, metadata, created_at
                FROM audit_log WHERE company_id = :c ORDER BY id DESC LIMIT :l
                """
            ),
            {"c": company_id, "l": limit},
        )
        out = [
            {
                "actor": r.actor,
                "action": r.action,
                "target": r.target,
                "metadata": r.metadata if isinstance(r.metadata, dict) else json.loads(r.metadata),
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    return {"count": len(out), "entries": out}
