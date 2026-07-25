from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import quote

from arq import create_pool
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.auth_routes import router as auth_router
from apps.common.analysis import (
    ASSIGN_PURPOSE,
    accept_assignment,
    complete_ticket,
    finish_create_chain,
    free_members,
    propose_assignment,
    request_action,
    request_pr_review,
    review_findings,
    review_one_record,
    reviewable_types,
    run_analysis,
)
from apps.common.assistant_tools import answer, answer_streaming, history_for_model
from apps.common.clarifications import resolve_clarification
from apps.common.context import (
    DRY_RUN_KEY,
    ProfileNotFound,
    approval_policy,
    get_profile,
    save_team_roster,
    team_roster,
)
from apps.common.discovery_flow import (
    confirm_proposed,
    list_versions,
    propose_from_connector,
)
from apps.common.feed_stream import CHANNEL as FEED_CHANNEL
from apps.common.inbound import handle_inbound_reply
from apps.common.ingestion import push_events, trigger_backfill, trigger_ingest
from apps.common.scheduling import ingest_timeout
from apps.common.understanding import describe
from apps.common.webhooks import handle_github_webhook
from apps.common.workflows_flow import (
    _acts_live,
    apps_used,
    edit_workflow,
    plan_workflow,
    run_workflow,
    step_app,
)
from packages.connectors.base import (
    SUPPORTED_SOURCES,
    list_oauth_targets,
    oauth_provider_for,
)
from packages.connectors.github import EVENT_HEADER, SIGNATURE_HEADER
from packages.core import audit, graph
from packages.core import connector_health as ch
from packages.core import workflows as wf
from packages.core.act import decide, list_actions
from packages.core.briefing import build_briefing
from packages.core.conversations import (
    add_message,
    conversation_exists,
    create_conversation,
    get_or_create_default_conversation,
    list_conversations,
    list_messages,
)
from packages.core.credentials import (
    delete_credential,
    get_credential,
    list_connections,
    save_credential,
)
from packages.core.db import Session
from packages.core.erasure import (
    confirm_deletion_request,
    create_deletion_request,
    get_latest_deletion_request,
)
from packages.core.items import item_facets, list_items
from packages.core.norms import get_norms, norm_evidence, reset_norms
from packages.core.oauth import OAuthError, authorize_url, exchange_code
from packages.core.pipeline import redis_settings
from packages.core.profile import (
    Profile,
    load_profile,
    save_profile,
    set_autonomy,
    set_source_enabled,
)
from packages.core.review import record_dismissal, suppression_report
from packages.core.search import search
from packages.core.settings import set_setting
from packages.core.situations import (
    ack_situation,
    dismiss_situation,
    get_situation,
    list_situations,
)
from packages.core.store import get_events_by_ids
from packages.core.tenancy import company_scope
from packages.core.tickets import close_ticket, get_ticket, list_tickets
from packages.core.tokens import consume_token, issue_token, peek_token
from packages.core.webhooks import verify_signature
from packages.core.workload import open_workload
from packages.shared.schema import TeamMember, WorkflowStep, WorkflowTrigger


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Graph constraints/indexes/vector index are created idempotently on every
    # boot. Nothing else is planted: a company's profile is DISCOVERED from its
    # own data once a source is connected, never seeded from a file.
    await graph.bootstrap()
    yield
    await graph.close_driver()


app = FastAPI(title="MarkOS", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# Identity. Registered first so /api/auth/* is reachable without a session;
# every other route below depends on company_scope and 401s without one.
app.include_router(auth_router)


async def profile_scope(company_id: str = Depends(company_scope)) -> Profile:
    """Load the company's confirmed profile — the only door for domain data."""
    async with Session() as session:
        try:
            return await get_profile(session, company_id)
        except ProfileNotFound as exc:
            raise HTTPException(404, str(exc)) from exc


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ------------------------------ Webhooks ------------------------------


@app.post("/api/webhooks/github")
async def github_webhook(request: Request, company_id: str = Query("default")) -> dict:
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
        profile = await get_profile(session, company_id)
        result = await handle_github_webhook(session, profile, event_type, payload)
        await audit.record(
            session, company_id, "github-webhook", "webhook.received",
            target=event_type, metadata=result,
        )
        await session.commit()
    return {"ok": True, **result}


# ---------------------------- Briefing -----------------------------


@app.get("/api/briefing")
async def briefing(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """Workspace briefing: generic state summary + the profile's briefing policy."""
    async with Session() as session:
        return await build_briefing(
            session, company_id, profile.vocabulary.get("briefing_policy", [])
        )


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
    """Rendered when the approver clicks the link. Does NOT consume the token —
    mail scanners pre-fetch links, and a GET must never change anything."""
    async with Session() as session:
        payload = await peek_token(session, token, ASSIGN_PURPOSE)
    if payload is None:
        return _page("Link expired", "<p>This link is invalid, already used, or expired.</p>", "#d1242f")
    return _page(
        "Confirm assignment",
        f"""<p>Assign <b>{payload.get('item_label', 'this work')}</b> to
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
    """Burns the single-use token, runs the profile's assign move, opens a ticket."""
    async with Session() as session:
        payload = await consume_token(session, token, ASSIGN_PURPOSE, used_by="email-link")
        if payload is None:
            await session.commit()
            return _page("Link expired", "<p>This link is invalid, already used, or expired.</p>", "#d1242f")
        company_id = payload["company_id"]
        profile = await get_profile(session, company_id)
        result = await accept_assignment(session, profile, payload, clicked_by="approver")
        await audit.record(
            session, company_id, "approver", "assignment.accepted",
            target=payload["assignee"], metadata={"ticket_id": result["ticket_id"],
                                                  "situation_id": payload["situation_id"]},
        )
        await session.commit()

    note = "" if not result["dry_run"] else "<p style='color:#bf8700'>Practice mode: nothing external was updated.</p>"
    return _page(
        "Assigned",
        f"""<p><b>{result['assignee']}</b> now owns
            <b>{payload.get('item_label', 'this work')}</b>.</p>
        <p>Ticket <b>#{result['ticket_id']}</b> was created for them.</p>
        <p style="color:#57606a;font-size:13px">Source: {result['assign_status']} - {result['detail']}</p>
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
    profile: Profile = Depends(profile_scope),
) -> dict:
    """The assignee finished. Close the ticket, then close the source item."""
    by = str(payload.get("by") or "assignee")
    async with Session() as session:
        ticket = await get_ticket(session, company_id, ticket_id)
        if ticket is None:
            raise HTTPException(404, f"no ticket {ticket_id}")
        if ticket.status == "done":
            raise HTTPException(400, "ticket already closed")

        closed = await close_ticket(session, company_id, ticket_id)
        source = await complete_ticket(session, profile, ticket, by)
        await audit.record(session, company_id, by, "ticket.closed", target=str(ticket_id),
                           metadata={"source": source["source"]})
        await session.commit()
    return {"ticket": closed.model_dump(mode="json") if closed else None, "source": source}


# ------------------------------- Team --------------------------------


@app.get("/api/team")
async def get_team(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """The roster, annotated with each person's live workload and availability."""
    team = profile.moves.get("team", {}) or {}
    async with Session() as session:
        roster = await team_roster(session, company_id)
        workload = await open_workload(session, company_id, team.get("workload", {}))
    free = {m["id"] for m in free_members(profile, roster, workload)}
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
        "roles": team.get("roles", []),
        "notify_roles": team.get("notify_roles", []),
        "assignable_role": team.get("assignable_role", ""),
    }


def _validate_member(payload: dict, roles: list[str]) -> dict:
    try:
        member = TeamMember(**payload)
    except Exception as exc:
        raise HTTPException(400, f"invalid member: {exc}") from exc
    if "@" not in member.email:
        raise HTTPException(400, "email must be a real address")
    unknown = [r for r in member.roles if r not in roles]
    if unknown:
        raise HTTPException(400, f"unknown role(s): {unknown}. allowed: {roles}")
    if not member.roles:
        raise HTTPException(400, f"give the member at least one role: {roles}")
    if member.max_open_issues < 1:
        raise HTTPException(400, "max_open_issues must be at least 1")
    return member.model_dump()


@app.post("/api/team")
async def upsert_member(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    roles = (profile.moves.get("team", {}) or {}).get("roles", [])
    member = _validate_member(payload, roles)
    async with Session() as session:
        roster = await team_roster(session, company_id)
        roster = [m for m in roster if m["id"] != member["id"]] + [member]
        await save_team_roster(session, company_id, roster)
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
        await save_team_roster(session, company_id, roster)
        await audit.record(session, company_id, "ui", "team.member.removed", target=member_id)
        await session.commit()
    return {"members": roster}


# ----------------------------- Connect -----------------------------


@app.get("/api/connections")
async def get_connections(company_id: str = Depends(company_scope)) -> dict:
    """What this workspace CAN connect, and what it already has.

    The list comes from the connector registry, never from the profile. Deriving
    it from the profile was a deadlock: a new workspace has no profile, a
    profile is discovered from events, and events only arrive once a source is
    connected — so the one screen you need on day one showed nothing to click.
    Capability belongs to the connectors, the same rule the action registry
    already follows.
    """
    supported = list(SUPPORTED_SOURCES)
    async with Session() as session:
        conns = await list_connections(session, company_id)
        profile = await load_profile(session, company_id)
    # a profile may name a source the registry doesn't ship (a push-only feed)
    for source in (profile.sources if profile else []):
        name = str(source.get("source") or "")
        if source.get("kind") == "connector" and name and name not in supported:
            supported.append(name)
    connected = {c.source: c for c in conns}
    def _oauth_state(source: str) -> dict:
        """Whether this source offers "sign in with…" — asked of the connector
        registry, so the UI never hardcodes which tools have OAuth."""
        provider = oauth_provider_for(source)
        return {
            "oauth": provider is not None,
            # configured=False means the operator hasn't registered an OAuth
            # app yet; the UI falls back to the token field and says why
            "oauth_configured": bool(provider and provider.configured),
        }

    return {
        "connections": [
            {
                "source": s,
                "connected": s in connected,
                "config": connected[s].config if s in connected else {},
                **_oauth_state(s),
            }
            for s in supported
        ]
    }


# ------------------------------ OAuth connect ------------------------------
# "Sign in with GitHub" instead of pasting a Personal Access Token. The token
# lands in exactly the same sealed credential store a pasted one does, so
# nothing downstream changes.

OAUTH_PURPOSE = "oauth_state"
_OAUTH_STATE_TTL_MINUTES = 10


def _oauth_redirect_uri(source: str) -> str:
    """Where the provider sends the human back. Must byte-match the callback
    URL registered on the provider's side."""
    return f"{os.getenv('PUBLIC_BASE_URL', 'http://localhost:8000').rstrip('/')}/api/oauth/{source}/callback"


def _web_url(params: str = "") -> str:
    """A FIXED destination for post-callback redirects. Deliberately not taken
    from a query parameter — echoing a caller-supplied URL here is exactly how
    an OAuth callback becomes an open redirect."""
    return f"{os.getenv('WEB_BASE_URL', 'http://localhost:3000').rstrip('/')}/{params}"


def _oauth_provider_or_400(source: str):
    provider = oauth_provider_for(source)
    if provider is None:
        raise HTTPException(400, f"{source} has no OAuth flow — connect it with a token instead")
    if not provider.configured:
        raise HTTPException(
            503,
            f"{source} OAuth is not configured. Register an OAuth app with callback "
            f"{_oauth_redirect_uri(source)} and set {source.upper()}_CLIENT_ID / "
            f"{source.upper()}_CLIENT_SECRET.",
        )
    return provider


@app.get("/api/oauth/{source}/start")
async def oauth_start(source: str, company_id: str = Depends(company_scope)) -> RedirectResponse:
    """Send the human to the provider to say yes.

    `state` is a single-use, expiring, sealed token (the same machinery the
    one-click email links use). It carries the company_id, so the callback
    learns WHO this grant belongs to from a value it minted itself rather than
    trusting a query parameter an attacker could set.
    """
    provider = _oauth_provider_or_400(source)
    async with Session() as session:
        state = await issue_token(
            session, company_id, OAUTH_PURPOSE, {"source": source},
            ttl_minutes=_OAUTH_STATE_TTL_MINUTES,
        )
        await audit.record(session, company_id, "ui", "oauth.started", target=source)
        await session.commit()
    return RedirectResponse(
        authorize_url(provider, state=state, redirect_uri=_oauth_redirect_uri(source)),
        status_code=307,
    )


@app.get("/api/oauth/{source}/callback")
async def oauth_callback(
    source: str,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
) -> RedirectResponse:
    """Where the provider drops the human back. Burns the state, trades the
    code for a token server-side, and seals it into the credential store.

    Note there is no company_scope here: the company comes from the state we
    issued, never from the URL.
    """
    if error:
        return RedirectResponse(_web_url(f"?connect_error={quote(error)}"), status_code=303)
    if not code or not state:
        return RedirectResponse(_web_url("?connect_error=missing_code"), status_code=303)

    provider = _oauth_provider_or_400(source)
    async with Session() as session:
        body = await consume_token(session, state, OAUTH_PURPOSE, used_by="oauth-callback")
        if body is None or body.get("source") != source:
            await session.commit()
            # expired, replayed, forged, or for a different source
            return RedirectResponse(_web_url("?connect_error=invalid_state"), status_code=303)
        company_id = str(body["company_id"])

        try:
            token = await exchange_code(provider, code, _oauth_redirect_uri(source))
        except OAuthError as exc:
            await session.commit()
            return RedirectResponse(_web_url(f"?connect_error={quote(str(exc))}"), status_code=303)

        # keep any config already chosen (e.g. the repo) across a re-auth
        existing = await get_credential(session, company_id, source)
        config = existing[1] if existing else {}
        await save_credential(session, company_id, source, token, config)
        await audit.record(
            session, company_id, "oauth", "connection.saved", target=source,
            # the token itself is NEVER recorded — only that a grant happened
            metadata={"via": "oauth", "scope": provider.scope},
        )
        await session.commit()
    return RedirectResponse(_web_url(f"?connected={source}"), status_code=303)


@app.get("/api/oauth/{source}/targets")
async def oauth_targets(source: str, company_id: str = Depends(company_scope)) -> dict:
    """What the granted token can watch (e.g. repos), so the UI offers a
    picker instead of a free-text `owner/name` field."""
    async with Session() as session:
        cred = await get_credential(session, company_id, source)
    if cred is None or not cred[0]:
        raise HTTPException(400, f"{source} is not connected")
    try:
        targets = await list_oauth_targets(source, cred[0])
    except Exception as exc:
        raise HTTPException(502, f"could not list {source} targets: {exc}") from exc
    return {"source": source, "targets": targets, "selected": cred[1].get("repo")}


@app.post("/api/connections/{source}")
async def connect(
    source: str,
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
) -> dict:
    """Save a connection.

    What may be connected is whatever the CONNECTOR REGISTRY ships, plus
    anything extra the profile names. The profile can only widen that set,
    never narrow it: a new workspace has an empty profile, and letting an
    empty profile veto the registry made every first connection fail with
    "unsupported source" — the same deadlock the GET route had.
    """
    async with Session() as session:
        existing = await load_profile(session, company_id)
    supported = set(SUPPORTED_SOURCES)
    if existing is not None:
        supported |= {
            str(s.get("source")) for s in existing.sources if s.get("kind") == "connector"
        }
    if source not in supported:
        raise HTTPException(400, f"unsupported source: {source}")
    token = payload.pop("token", "") or ""

    # Where a real sign-in exists, it is the ONLY way in. A pasted token is a
    # long-lived secret this system then has to hold, with whatever breadth the
    # person happened to grant it and no way to tell whose it is; the OAuth
    # grant is scoped, attributable and revocable from the provider's own
    # settings page. Enforced HERE rather than by hiding the field, because a
    # control that is merely invisible is still an endpoint — and this one
    # writes a credential.
    #
    # Sources with no OAuth app configured are unaffected: for them a token is
    # the only way to connect at all, and refusing it would just mean nobody
    # can connect anything.
    provider = oauth_provider_for(source)
    if token and provider is not None and provider.configured:
        raise HTTPException(
            400,
            f"{source} connects by signing in, not with a pasted token — "
            f"use Sign in with {source.capitalize()} so access stays scoped and revocable.",
        )

    async with Session() as session:
        # An empty token means "I'm only changing config" (e.g. picking which
        # repo to watch after an OAuth grant) — NOT "throw my token away".
        # Keep whatever is already sealed; clearing a credential is what the
        # DELETE endpoint is for.
        if not token:
            current = await get_credential(session, company_id, source)
            token = current[0] if current else ""
        await save_credential(session, company_id, source, token, payload)
        await audit.record(session, company_id, "api", "connection.saved", target=source,
                           metadata={"config": payload})
        await session.commit()

    # Connecting a tool has to be enough. Until now the profile was seeded from
    # a file, so a connection was the only missing piece; with discovery it is
    # the FIRST piece, and leaving induction to a button nobody knew to press
    # meant a connected workspace stayed permanently empty. Discovery validates
    # itself — a proposal that cannot normalize its own sample payloads is
    # rejected — so a proposal that survives that is safe to activate.
    discovered = None
    if existing is None or not any(s.get("source") == source for s in existing.sources):
        try:
            async with Session() as session:
                proposal = await propose_from_connector(session, company_id, source, limit=30)
                await session.commit()
            async with Session() as session:
                await confirm_proposed(session, company_id, proposal["version"])
                await session.commit()
            discovered = {"version": proposal["version"], "things": proposal.get("things", [])}
        except Exception as exc:
            # the connection itself is saved and valid; discovery can be retried
            discovered = {"error": str(exc)}

    # Learning the SHAPE of the data is not the same as having any. Discovery
    # alone left a freshly connected workspace empty until the next scan tick,
    # and left the agent blind to everything opened or closed before we arrived.
    # Queued rather than awaited: a history walk paces itself across pages and
    # has no business holding an HTTP request open.
    syncing = False
    try:
        pool = await create_pool(redis_settings())
        try:
            await pool.enqueue_job("first_sync", company_id, source)
            syncing = True
        finally:
            await pool.aclose()
    except Exception:
        # the connection is still real; the scan cron will pick it up
        syncing = False

    return {
        "source": source, "connected": True, "config": payload,
        "discovered": discovered, "syncing": syncing,
    }


@app.delete("/api/connections/{source}")
async def disconnect(source: str, company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        await delete_credential(session, company_id, source)
        await audit.record(session, company_id, "api", "connection.removed", target=source)
        await session.commit()
    return {"source": source, "connected": False}


@app.post("/api/connections/{source}/watching")
async def set_watching(
    source: str,
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
) -> dict:
    """Resume (or stop) watching a source. The clarification card's "this is
    expected, stop watching" choice disables a source; this is how it — and
    its sibling "help me reconnect" — get undone, as a new profile version."""
    if "enabled" not in payload:
        raise HTTPException(400, 'body must be {"enabled": true|false}')
    enabled = bool(payload["enabled"])
    async with Session() as session:
        updated = await set_source_enabled(session, company_id, source, enabled)
        if updated is None:
            raise HTTPException(404, f"no source {source!r} in this company's profile")
        await audit.record(
            session, company_id, "ui", "connection.watching",
            target=source, metadata={"enabled": enabled, "profile_version": updated.version},
        )
        await session.commit()
    return {"source": source, "enabled": enabled, "profile_version": updated.version}


@app.post("/api/connections/{source}/sync")
async def sync(
    source: str,
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    async with Session() as session:
        try:
            counts = await trigger_ingest(session, profile, only_source=source)
        except Exception as exc:  # surface connector errors to the UI
            raise HTTPException(502, f"{source} sync failed: {exc}") from exc
        await audit.record(session, company_id, "api", "ingest.trigger", target=source,
                           metadata=counts)
        await session.commit()
    return {"enqueued": counts}


@app.post("/api/connections/{source}/backfill")
async def backfill(
    source: str,
    since_days: int = Query(90, ge=1, le=730),
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """Checkpoint 2, part A: walk this source's real history (not just the
    live sync's recent window) so norms have real depth to learn from.
    Queued as a low-priority worker job — see packages.core.pipeline.backfill_source."""
    async with Session() as session:
        queued = await trigger_backfill(session, profile, only_source=source, since_days=since_days)
        if not queued:
            raise HTTPException(400, f"{source} is not connected")
        await audit.record(session, company_id, "api", "ingest.backfill", target=source,
                           metadata={"since_days": since_days})
        await session.commit()
    return {"queued": queued, "since_days": since_days}


@app.post("/api/ingest")
async def ingest_all(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    async with Session() as session:
        counts = await trigger_ingest(session, profile)
        await audit.record(session, company_id, "api", "ingest.trigger", metadata=counts)
        await session.commit()
    return {"enqueued": counts}


@app.post("/api/ingest/push")
async def ingest_push(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """Generic push feed for `kind: push` profile sources.

    Body: {"source": "<push source name>", "events": [<raw payload>, ...]}
    Payloads ride the same pipeline as connector pulls.
    """
    source = str(payload.get("source", ""))
    raws = payload.get("events", [])
    if not source or not isinstance(raws, list) or not raws:
        raise HTTPException(400, 'body must be {"source": ..., "events": [...]}')
    try:
        count = await push_events(profile, source, raws)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    async with Session() as session:
        await audit.record(session, company_id, "api", "ingest.push", target=source,
                           metadata={"count": count})
        await session.commit()
    return {"enqueued": {source: count}}


# ------------------------------ Events ------------------------------


@app.get("/api/events")
async def events(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
    source: str | None = None,
    type: str | None = None,
    state: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """Raw events. ``state`` filters on whatever field the PROFILE says holds
    a record's status (`things.status_field`) — this used to hardcode
    `metadata->>'state'`, which is GitHub's word and silently matched nothing
    for a profile that calls it `status`."""
    status_field = (profile.things or {}).get("status_field")
    async with Session() as session:
        rows = await list_items(
            session, company_id, status_field=status_field,
            source=source, type_=type, status=state, limit=limit,
        )
        raw = await session.execute(
            text("SELECT id, content, metadata FROM events WHERE company_id = :c AND id = ANY(:ids)"),
            {"c": company_id, "ids": [r["id"] for r in rows]},
        )
    detail = {
        r.id: (r.content, r.metadata if isinstance(r.metadata, dict) else json.loads(r.metadata))
        for r in raw
    }
    out = [
        {
            "id": r["id"],
            "source": r["source"],
            "type": r["type"],
            "actor": {"id": r["actor"], "name": r["actor"]},
            "timestamp": r["timestamp"],
            "content": detail.get(r["id"], ("", {}))[0],
            "metadata": detail.get(r["id"], ("", {}))[1] or {},
        }
        for r in rows
    ]
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
    result = await graph.query_graph(company_id, event_id, hops)
    return result.model_dump()


@app.get("/api/graph/stats")
async def graph_stats(company_id: str = Depends(company_scope)) -> dict:
    return await graph.count_nodes(company_id)


@app.get("/api/norms")
async def norms(company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        baselines = await get_norms(session, company_id)
    return {"company_id": company_id, "norms": [b.model_dump() for b in baselines]}


# ------------------------------ Alert -------------------------------


@app.post("/api/analyze")
async def analyze(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """Learn norms -> detect situations -> assemble briefs -> deliver.

    Re-reasons over what has already been ingested; it does NOT poll. Use
    /api/scan for the "check my tools again" button.
    """
    async with Session() as session:
        summary = await run_analysis(session, profile)
        await audit.record(session, company_id, "api", "analyze.run", metadata=summary)
        await session.commit()
    return summary


@app.post("/api/scan")
async def scan_now(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """Poll every connected source, then reason over what came back.

    What "Scan now" always claimed to do and didn't: it called /api/analyze,
    which only re-reasons over events already stored. Sitting next to "N events
    watched", a button reading "Scan now" that cannot discover a single new
    record is a promise the product does not keep — a pull request opened
    minutes earlier stayed invisible until the 15-minute cron happened to run.

    Deliberately the same two steps, in the same order, as the unattended scan
    (apps.common.scheduling.scheduled_scan): fetch and WAIT for the queue to
    drain, then analyse. Waiting is what makes the button honest — detection
    reads events from the store, so returning before they land would report on
    the state that existed before the click.
    """
    async with Session() as session:
        ingested = await trigger_ingest(session, profile, wait=ingest_timeout())
        summary = await run_analysis(session, profile)
        summary["ingested"] = ingested
        await audit.record(session, company_id, "ui", "scan.run", metadata=summary)
        await session.commit()
    return summary


@app.get("/api/situations")
async def situations(
    company_id: str = Depends(company_scope),
    include_system: bool = Query(False, description="include kind=system self-monitoring situations"),
) -> dict:
    async with Session() as session:
        items = await list_situations(session, company_id, include_system=include_system)
    return {"count": len(items), "situations": [s.model_dump(mode="json") for s in items]}


@app.post("/api/situations/{situation_id}/ack")
async def ack_situation_route(
    situation_id: str, company_id: str = Depends(company_scope)
) -> dict:
    async with Session() as session:
        ok = await ack_situation(session, company_id, situation_id)
        if not ok:
            raise HTTPException(404, "no open situation with that id")
        await audit.record(session, company_id, "ui", "situation.ack", target=situation_id)
        await session.commit()
    return {"status": "acknowledged", "situation_id": situation_id}


@app.post("/api/situations/{situation_id}/dismiss")
async def dismiss_situation_route(
    situation_id: str, company_id: str = Depends(company_scope)
) -> dict:
    async with Session() as session:
        # Record the dismissal BEFORE resolving it: record_dismissal reads the
        # situation's rule/category, and a review finding teaches the reviewer
        # not to raise this (or its whole category) again — the feedback loop.
        # A no-op for any non-review situation.
        await record_dismissal(session, company_id, situation_id)
        ok = await dismiss_situation(session, company_id, situation_id)
        if not ok:
            raise HTTPException(404, "no open situation with that id")
        await audit.record(session, company_id, "ui", "situation.dismiss", target=situation_id)
        await session.commit()
    return {"status": "dismissed", "situation_id": situation_id}


@app.get("/api/reviews/suppressions")
async def review_suppressions_route(
    company_id: str = Depends(company_scope),
) -> dict:
    """What the reviewer has learned to stop raising, and the dismissals that
    taught it — evidence, never a silent filter."""
    async with Session() as session:
        muted = await suppression_report(session, company_id)
    return {"count": len(muted), "muted": muted}


@app.post("/api/inbound/email")
async def inbound_email_route(
    payload: dict = Body(...),
    secret: str = Query("", description="shared secret the email forwarder includes"),
) -> dict:
    """Receive one inbound email reply from an email-forwarding service and, if a
    lead asked to reassign, act on it. This is the loop's other half: the system
    doesn't just email people, it reads what they email back.

    NOT behind the workspace login — an email forwarder is not a signed-in user.
    The workspace is resolved from the [ref:…] tag in the subject, and the
    endpoint is gated by INBOUND_EMAIL_SECRET so only your configured forwarder
    can reach it. Disabled (503) until that secret is set."""
    expected = os.getenv("INBOUND_EMAIL_SECRET", "")
    if not expected:
        raise HTTPException(503, "inbound email not enabled — set INBOUND_EMAIL_SECRET")
    if secret != expected:
        raise HTTPException(403, "bad or missing inbound secret")
    async with Session() as session:
        result = await handle_inbound_reply(session, payload)
        await session.commit()
    return result


@app.get("/api/reviews/{thing_id}")
async def get_review_route(
    thing_id: str, company_id: str = Depends(company_scope),
) -> dict:
    """A record's current review findings — what the reviewer saw, each at its
    exact file:line. Empty until the record is reviewed."""
    async with Session() as session:
        findings = await review_findings(session, company_id, thing_id)
    return {"thing_id": thing_id, "count": len(findings), "findings": findings}


@app.post("/api/reviews/{thing_id}/run")
async def run_review_route(
    thing_id: str,
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """Review this record now — what a Review button calls. Reads the code,
    fans the concerns out, raises findings. Nothing is posted; findings surface
    for a person to read (and to Request changes on, gated)."""
    async with Session() as session:
        result = await review_one_record(session, profile, thing_id, force=True)
        if result is None:
            raise HTTPException(404, "no reviewer targets this record's type")
        await audit.record(session, company_id, "ui", "review.run", target=thing_id, metadata=result)
        await session.commit()
        findings = await review_findings(session, company_id, thing_id)
    return {"thing_id": thing_id, "review": result, "count": len(findings), "findings": findings}


# ------------------------------ Feed (CP2 part C/D) ------------------------------


@app.get("/api/feed")
async def feed(
    company_id: str = Depends(company_scope),
    include_system: bool = Query(False),
) -> dict:
    """The watcher engine's output, ready for the Feed page: situations +
    pending/recent actions in one round trip."""
    async with Session() as session:
        situations = await list_situations(session, company_id, include_system=include_system)
        actions = await list_actions(session, company_id)
    return {
        "situations": [s.model_dump(mode="json") for s in situations],
        "actions": actions,
    }


@app.get("/api/items")
async def items(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
    source: str | None = None,
    type: str | None = None,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    """The work itself, with facet counts — the answer to "what have I got?",
    as opposed to /api/feed's "what needs me?".

    Which metadata key holds a record's status is PROFILE data
    (`things.status_field`), so this endpoint never knows that GitHub calls it
    "state" and an inventory profile calls it "status". The label the UI shows
    for one record comes from the profile's vocabulary too.
    """
    status_field = (profile.things or {}).get("status_field")
    async with Session() as session:
        rows = await list_items(
            session, company_id, status_field=status_field,
            source=source, type_=type, status=status, limit=limit,
        )
        facets = await item_facets(
            session, company_id, status_field=status_field,
            source=source, type_=type, status=status,
        )
    return {
        "items": rows,
        "facets": facets,
        # what this company calls one record ("issue", "order", ...) so the UI
        # can label the view without guessing
        "noun": (profile.vocabulary.get("terms", {}) or {}).get("thing", "item"),
    }


@app.get("/api/learning")
async def learning(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """What the system learned, WITH the working shown.

    Each baseline comes back with the real records that produced it, which
    ones were discarded as outliers, and the line past which it raises an
    alert — so a person can check the maths against their own work instead of
    taking a number on trust. Also returns what was inferred about the shape
    of the data (which field means "finished", who the actor is), and the size
    of the knowledge graph built from it.
    """
    async with Session() as session:
        measurements = [
            await norm_evidence(session, company_id, defn) for defn in profile.rhythms
        ]
        facets = await item_facets(
            session, company_id, status_field=(profile.things or {}).get("status_field")
        )
    graph_counts = await graph.count_nodes(company_id)

    source_defs = {s["source"]: s for s in profile.sources if s.get("kind") == "connector"}
    inferred = []
    for rhythm in profile.rhythms:
        mapping = (source_defs.get(rhythm.get("source"), {}) or {}).get("mapping", {})
        inferred.append(
            {
                "type": rhythm.get("type"),
                "source": rhythm.get("source"),
                "finished_when": rhythm.get("end_field"),
                "timestamp_from": (mapping.get("timestamp") or {}).get("path"),
                "actor_from": (mapping.get("actor_name") or {}).get("path"),
                "status_from": (profile.things or {}).get("status_field"),
            }
        )

    return {
        "measurements": measurements,
        "inferred": inferred,
        "records": {"total": sum(f["count"] for f in facets["sources"]), "by_type": facets["types"]},
        "graph": {
            "things": graph_counts.get("Thing", 0),
            "events": graph_counts.get("Event", 0),
            "total": graph_counts.get("total", 0),
        },
        "terms": profile.vocabulary.get("terms", {}) or {},
        "profile_version": profile.version,
    }


@app.get("/api/knowledge")
async def knowledge(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """The company as the system sees it: the profile as readable data, plus
    the real graph built from real events.

    This is a VIEWER, not an editor. The seven slots are rendered as facts
    ("you have 4 kinds of thing, here they are") — never as form fields, which
    would just be YAML in a browser. Changing any of it goes through the agent,
    which records who changed what and why.
    """
    picture = await graph.company_graph(company_id)
    async with Session() as session:
        company_name = (
            await session.execute(
                text("SELECT name FROM companies WHERE id = :company_id"),
                {"company_id": company_id},
            )
        ).scalar_one_or_none()
    things_slot = profile.things or {}
    links_slot = profile.links or {}

    # Which thing types actually showed up in the data, vs merely declared.
    seen: dict[str, int] = {}
    for t in picture["things"]:
        key = t["thing_type"] or "unknown"
        seen[key] = seen.get(key, 0) + 1

    return {
        "company": {
            "id": company_id,
            "name": company_name or company_id,
            "version": profile.version,
            "status": profile.status,
        },
        "sources": [
            {
                "source": s.get("source"),
                "kind": s.get("kind"),
                "enabled": s.get("enabled", True),
                "produces": [
                    t.get("event_type")
                    for t in things_slot.get("types", [])
                    if t.get("source") == s.get("source")
                ],
            }
            for s in profile.sources
        ],
        "thing_types": [
            {
                "name": t.get("name"),
                "source": t.get("source"),
                "from_event": t.get("event_type"),
                "count": seen.get(t.get("name"), 0),
            }
            for t in things_slot.get("types", [])
        ],
        "link_types": links_slot.get("types", []),
        "rhythms": [
            {"name": r.get("name"), "type": r.get("type"), "finished_when": r.get("end_field")}
            for r in profile.rhythms
        ],
        "terms": profile.vocabulary.get("terms", {}) or {},
        "graph": picture,
    }


@app.get("/api/knowledge/things/{thing_id:path}")
async def knowledge_thing(
    thing_id: str,
    company_id: str = Depends(company_scope),
) -> dict:
    """One thing's own page: its neighbours and every event about it.

    The graph mirror holds ids and times only, so the event *content* is
    joined back from Postgres here — the mirror stays thin and cannot drift.
    """
    detail = await graph.thing_detail(company_id, thing_id)
    if not detail:
        raise HTTPException(404, "thing not found")

    event_ids = [e["event_id"] for e in detail["events"]]
    if event_ids:
        async with Session() as session:
            rows = await get_events_by_ids(session, company_id, event_ids)
        by_id = {e.id: e for e in rows}
        for e in detail["events"]:
            full = by_id.get(e["event_id"])
            if full is not None:
                e["content"] = full.content
                e["actor_name"] = full.actor.name
                e["type"] = full.type
                e["url"] = (full.metadata or {}).get("url")
    return detail


@app.get("/api/understanding")
async def understanding(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """Everything the system knows about this company, in one answer — what
    it watches, what it learned, what it checks for, what it may do, and
    whether Practice mode is protecting you. All of this existed already but
    had no screen, which is what made the product feel unknowable."""
    async with Session() as session:
        return await describe(session, profile)


@app.get("/api/feed/stream")
async def feed_stream(company_id: str = Depends(company_scope)) -> StreamingResponse:
    """SSE: a nudge to refetch /api/feed, published (Redis pub/sub) every
    time the watcher engine finishes a pass for this company — whichever
    cadence triggered it (webhook, the business scan, or the watcher
    engine's own 5-min cron). Carries no situation data itself, so it can
    never drift out of sync with Postgres, the system of record."""

    async def events():
        pool = await create_pool(redis_settings())
        pubsub = pool.pubsub()
        channel = FEED_CHANNEL.format(company_id=company_id)
        await pubsub.subscribe(channel)
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=25.0)
                if message is None:
                    yield ": keep-alive\n\n"  # SSE comment — holds the connection through proxies
                    continue
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode()
                yield f"data: {data}\n\n"
        finally:
            await pubsub.unsubscribe(channel)
            await pool.aclose()

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)


@app.get("/api/connector-health")
async def connector_health_route(company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        rows = await ch.get_health(session, company_id)
    return {"company_id": company_id, "connectors": rows}


@app.post("/api/situations/{situation_id}/resolve")
async def resolve_situation_choice(
    situation_id: str,
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    choice = str(payload.get("choice") or payload.get("choice_id") or "")
    if not choice:
        raise HTTPException(400, "body must include choice")
    async with Session() as session:
        result = await resolve_clarification(
            session, profile, situation_id, choice, resolved_by=str(payload.get("by") or "ui")
        )
        if result["status"] == "not_found":
            raise HTTPException(404, "situation not found")
        if result["status"] == "invalid_choice":
            raise HTTPException(400, "invalid clarification choice")
        await session.commit()
    return result


@app.get("/api/conversations")
async def conversations(company_id: str = Depends(company_scope)) -> dict:
    """Every thread in this workspace, most recent first."""
    async with Session() as session:
        return {"conversations": await list_conversations(session, company_id)}


@app.post("/api/conversations")
async def new_conversation(
    payload: dict = Body(default={}),
    company_id: str = Depends(company_scope),
) -> dict:
    """Start a fresh thread.

    Every turn replays the last 20 messages, so without this a workspace had
    one endless conversation and no way out of it: an answer written when it
    was empty kept being fed back to the model days later.
    """
    async with Session() as session:
        conversation_id = await create_conversation(
            session, company_id, str(payload.get("title") or "New thread")
        )
        await session.commit()
    return {"conversation_id": conversation_id, "messages": []}


async def _resolve_conversation(
    session: AsyncSession, company_id: str, conversation_id: int | None
) -> int:
    """The thread to read or write, scoped to this workspace.

    A thread id belonging to somebody else must not resolve — the id is the
    only thing the client sends, so this is the whole check.
    """
    if conversation_id is None:
        return await get_or_create_default_conversation(session, company_id)
    if not await conversation_exists(session, company_id, int(conversation_id)):
        raise HTTPException(404, "no such conversation")
    return int(conversation_id)


@app.get("/api/conversations/default/messages")
async def default_messages(company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        conversation_id = await get_or_create_default_conversation(session, company_id)
        messages = await list_messages(session, company_id, conversation_id)
        await session.commit()
    return {
        "conversation_id": conversation_id,
        "messages": [m.model_dump(mode="json") for m in messages],
    }


@app.post("/api/conversations/default/messages")
async def post_default_message(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
) -> dict:
    """Persist one chat turn (user or agent). The Agent UI calls this for
    BOTH sides of every exchange, so a refresh never loses the thread —
    there is no local-only chat state left in the frontend."""
    role = str(payload.get("role") or "")
    if role not in ("user", "agent", "system"):
        raise HTTPException(400, 'role must be "user", "agent", or "system"')
    content = str(payload.get("content") or "")
    artifacts = payload.get("artifacts") or []
    if not isinstance(artifacts, list):
        raise HTTPException(400, "artifacts must be a list")
    async with Session() as session:
        conversation_id = await get_or_create_default_conversation(session, company_id)
        message = await add_message(session, conversation_id, role, content, artifacts)
        await session.commit()
    return message.model_dump(mode="json")


# Declared AFTER the literal "default" routes: FastAPI matches in declaration
# order, and an int-typed path param would reject "default" with a 422 rather
# than letting it fall through to the route that handles it.
@app.get("/api/conversations/{conversation_id}/messages")
async def conversation_messages(
    conversation_id: int, company_id: str = Depends(company_scope)
) -> dict:
    async with Session() as session:
        resolved = await _resolve_conversation(session, company_id, conversation_id)
        messages = await list_messages(session, company_id, resolved)
    return {
        "conversation_id": resolved,
        "messages": [m.model_dump(mode="json") for m in messages],
    }


# --------------------------- Profile discovery (CP5) ---------------------------


@app.get("/api/profile")
async def get_profile_route(company_id: str = Depends(company_scope)) -> dict:
    """The company's profile versions + whichever one is live."""
    async with Session() as session:
        versions = await list_versions(session, company_id)
        active = await load_profile(session, company_id)
    return {
        "company_id": company_id,
        "versions": versions,
        "active": active.model_dump(mode="json") if active else None,
    }


@app.post("/api/profile/induce")
async def induce_profile_route(
    payload: dict = Body(default={}),
    company_id: str = Depends(company_scope),
) -> dict:
    """Checkpoint 5: look at what a connected source really returns and
    PROPOSE a profile for this company. Saves a `proposed` version and
    activates nothing — the engine keeps running on the confirmed profile
    until a human calls /api/profile/confirm.

    Body: {"source": "github", "template_company_id": "<whose connection to
    read through, for a company that has none yet>", "limit": 30}
    """
    source = str(payload.get("source") or "")
    if not source:
        raise HTTPException(400, 'body must include "source"')
    async with Session() as session:
        try:
            result = await propose_from_connector(
                session, company_id, source,
                template_company_id=payload.get("template_company_id"),
                limit=int(payload.get("limit") or 30),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await audit.record(
            session, company_id, "api", "profile.induced", target=source,
            metadata={"version": result["version"], "valid": result["valid"]},
        )
        await session.commit()
    return result


@app.post("/api/profile/confirm")
async def confirm_profile_route(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
) -> dict:
    """The human's yes: promote a proposed profile to confirmed, as a new
    version (the prior versions are never overwritten)."""
    version = payload.get("version")
    if version is None:
        raise HTTPException(400, 'body must include "version"')
    async with Session() as session:
        try:
            profile = await confirm_proposed(session, company_id, int(version))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await audit.record(
            session, company_id, str(payload.get("by") or "ui"), "profile.confirmed",
            target=str(version), metadata={"confirmed_version": profile.version},
        )
        await session.commit()
    return {"status": "confirmed", "version": profile.version, "profile": profile.model_dump(mode="json")}


@app.post("/api/agent/chat")
async def agent_chat(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """Checkpoint 4: one turn of the real tool-use agent. The model grounds
    its answer in the company's data via read tools and can take action via
    run_action — which rides the same approval brake as the Feed buttons.

    Both turns are persisted through the same conversations/messages backbone
    the rest of the chat uses, so the thread survives a refresh and stays the
    single source of truth."""
    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "message is required")
    async with Session() as session:
        conversation_id = await _resolve_conversation(
            session, company_id, payload.get("conversation_id")
        )
        prior = await list_messages(session, company_id, conversation_id, limit=20)
        history = history_for_model(prior)
        user_msg = await add_message(session, conversation_id, "user", message, [])
        result = await answer(session, profile, message, history=history)
        agent_msg = await add_message(
            session, conversation_id, "agent", result["reply"], result["artifacts"]
        )
        await audit.record(
            session, company_id, "agent", "agent.chat", target=message[:120],
            metadata={"tools": [s["tool"] for s in result["steps"]]},
        )
        await session.commit()
    return {
        "reply": result["reply"],
        "artifacts": result["artifacts"],
        "message": agent_msg.model_dump(mode="json"),
        "user_message": user_msg.model_dump(mode="json"),
        # what the turn VERIFIABLY did, independent of what the reply claims
        "tools_used": result["tools_used"],
        "changed": result["changed"],
    }


# ------------------------------ Workflows (Phase 2) ------------------------------


def _with_apps(profile: Profile, workflow: wf.Workflow) -> dict:
    """A workflow serialized with its per-node app annotations — the 'which app
    for what' the n8n graph shows on each node."""
    data = workflow.model_dump(mode="json")
    data["apps_used"] = apps_used(profile, workflow.steps)
    # `acts_live` is DERIVED from the current registry on every read, not trusted
    # from what was stored — a workflow saved before this flag existed, or one
    # whose action's capability changed, still shows the truth about what it does
    # to your tools now.
    data["steps"] = [
        {**s.model_dump(mode="json"), "app_info": step_app(profile, s), "acts_live": _acts_live(profile, s)}
        for s in workflow.steps
    ]
    return data


@app.post("/api/workflows/plan")
async def plan_workflow_route(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """Compile a natural-language goal into a reviewable plan (trigger + steps +
    which app does what). Nothing is saved or run — the 'what I'm going to do'
    the user approves first."""
    goal = str(payload.get("goal") or "").strip()
    if not goal:
        raise HTTPException(400, "goal is required")
    async with Session() as session:
        plan = await plan_workflow(session, profile, goal)
    data = plan.model_dump(mode="json")
    data["apps_used"] = apps_used(profile, plan.steps)
    data["steps"] = [
        {**s.model_dump(mode="json"), "app_info": step_app(profile, s)} for s in plan.steps
    ]
    return data


@app.get("/api/workflows")
async def list_workflows_route(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    async with Session() as session:
        rows = await wf.list_workflows(session, company_id)
    return {"workflows": [_with_apps(profile, w) for w in rows]}


@app.post("/api/workflows")
async def create_workflow_route(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """Save a reviewed plan as a named, trigger-based workflow."""
    name = str(payload.get("name") or "").strip()
    goal = str(payload.get("goal") or "").strip()
    if not name or not goal:
        raise HTTPException(400, "name and goal are required")
    try:
        steps = [WorkflowStep(**s) for s in (payload.get("steps") or [])]
        trigger = WorkflowTrigger(**payload["trigger"]) if payload.get("trigger") else WorkflowTrigger()
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"invalid plan: {exc}") from exc
    async with Session() as session:
        workflow = await wf.save_workflow(
            session, company_id, name, goal, steps,
            trigger=trigger, created_by=str(payload.get("by") or "ui"),
        )
        await audit.record(
            session, company_id, str(payload.get("by") or "ui"), "workflow.saved",
            target=name, metadata={"workflow_id": workflow.id, "steps": len(steps), "trigger": trigger.type},
        )
        await session.commit()
    return _with_apps(profile, workflow)


@app.patch("/api/workflows/{workflow_id}")
async def update_workflow_route(
    workflow_id: int,
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    changes: dict = {k: payload[k] for k in ("name", "goal", "enabled") if k in payload}
    if "steps" in payload:
        try:
            changes["steps"] = [WorkflowStep(**s) for s in payload["steps"]]
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"invalid steps: {exc}") from exc
    if "trigger" in payload:
        try:
            trigger = WorkflowTrigger(**payload["trigger"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, f"invalid trigger: {exc}") from exc
        if trigger.type == "schedule" and not wf.valid_cron(str(trigger.config.get("cron", ""))):
            # refuse rather than accept-and-never-fire: a schedule nobody can
            # see failing is worse than an error at the moment it's set
            raise HTTPException(400, "That schedule isn't a valid cron expression.")
        changes["trigger"] = trigger
    async with Session() as session:
        updated = await wf.update_workflow(session, company_id, workflow_id, **changes)
        if updated is None:
            raise HTTPException(404, "workflow not found")
        # every write to a workflow is attributable. A workflow that quietly
        # changed its own schedule is exactly the kind of thing you can only
        # investigate if the edit left a trace.
        await audit.record(
            session, company_id, str(payload.get("by") or "ui"), "workflow.updated",
            target=updated.name,
            metadata={
                "workflow_id": workflow_id,
                "fields": sorted(changes.keys()),
                "trigger": updated.trigger.model_dump() if "trigger" in changes else None,
            },
        )
        await session.commit()
    return _with_apps(profile, updated)


# Headers every SSE response needs. `Content-Encoding: identity` is the load
# bearing one: a proxy in front of us (the Next dev server does this) will
# otherwise gzip the stream, and gzip buffers the whole body — the client then
# receives NOTHING until the response ends, which is the exact opposite of
# streaming. Declaring an encoding makes compressing middleware leave it alone.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",       # nginx
    "Content-Encoding": "identity",  # any gzip-ing proxy
}


def _sse(event: dict) -> str:
    """One Server-Sent Event frame. Kept as a helper because the streaming
    endpoints below all speak the same wire format the frontend parses once."""
    return f"data: {json.dumps(event, default=str)}\n\n"


@app.post("/api/agent/chat/stream")
async def agent_chat_stream(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> StreamingResponse:
    """The streaming twin of POST /api/agent/chat. Same turn, same persistence,
    same artifacts — the difference is you watch it happen: which tool is
    running, then the answer arriving word by word.

    The turn is persisted from the FINAL event, so what a refresh shows is
    identical to the non-streaming route. A client that drops mid-stream still
    gets a saved turn, because the write happens server-side, not on delivery.
    """
    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "message is required")

    async def events():
        async with Session() as session:
            conversation_id = await _resolve_conversation(
                session, company_id, payload.get("conversation_id")
            )
            prior = await list_messages(session, company_id, conversation_id, limit=20)
            history = history_for_model(prior)
            user_msg = await add_message(session, conversation_id, "user", message, [])
            await session.commit()
            yield _sse({"type": "user_message", "message": user_msg.model_dump(mode="json")})

            result: dict | None = None
            try:
                async for event in answer_streaming(session, profile, message, history=history):
                    if event["type"] == "final":
                        result = event
                    else:
                        yield _sse(event)
            except Exception as exc:  # never leave the client hanging on an open stream
                yield _sse({"type": "error", "error": str(exc)})
                return

            if result is None:
                yield _sse({"type": "error", "error": "the turn produced no answer"})
                return

            agent_msg = await add_message(
                session, conversation_id, "agent", result["reply"], result["artifacts"]
            )
            await audit.record(
                session, company_id, "agent", "agent.chat", target=message[:120],
                metadata={"tools": [s["tool"] for s in result["steps"]], "streamed": True},
            )
            await session.commit()
            yield _sse({
                "type": "final",
                "reply": result["reply"],
                "artifacts": result["artifacts"],
                "message": agent_msg.model_dump(mode="json"),
                "tools_used": result["tools_used"],
                "changed": result["changed"],
            })

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@app.post("/api/workflows/{workflow_id}/run/stream")
async def run_workflow_stream(
    workflow_id: int,
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> StreamingResponse:
    """Run a workflow and watch each node as it goes. A run can queue approvals
    or call a real API per step, so seeing WHICH step is live — and what each one
    decided — matters more here than in chat."""

    async def events():
        async with Session() as session:
            queue: asyncio.Queue = asyncio.Queue()

            async def on_event(kind, index, step, result):
                await queue.put({
                    "type": kind,
                    "index": index,
                    "tool": step.tool,
                    "action": step.args.get("action"),
                    "description": step.description,
                    "result": result.model_dump() if result is not None else None,
                })

            task = asyncio.create_task(
                run_workflow(session, profile, workflow_id, trigger="manual", on_event=on_event)
            )
            while not task.done() or not queue.empty():
                try:
                    yield _sse(await asyncio.wait_for(queue.get(), timeout=1.0))
                except TimeoutError:
                    yield ": keep-alive\n\n"
            try:
                yield _sse({"type": "final", **(await task)})
            except Exception as exc:
                yield _sse({"type": "error", "error": str(exc)})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@app.post("/api/workflows/{workflow_id}/edit")
async def edit_workflow_route(
    workflow_id: int,
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """The per-workflow edit chat: apply a natural-language instruction to THIS
    workflow ('add a step to assign it to me', 'change the trigger to every
    morning'). Re-plans and saves in place. Editing never runs anything — running
    stays separately gated — so applying an edit immediately is safe.

    If the instruction is ambiguous, returns clarifications and changes nothing."""
    instruction = str(payload.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(400, "instruction is required")
    async with Session() as session:
        workflow = await wf.get_workflow(session, company_id, workflow_id)
        if workflow is None:
            raise HTTPException(404, "workflow not found")
        plan = await edit_workflow(session, profile, workflow, instruction)
        if plan.clarifications:
            return {"applied": False, "clarifications": plan.clarifications}
        updated = await wf.update_workflow(
            session, company_id, workflow_id,
            name=plan.name, steps=plan.steps, trigger=plan.trigger,
        )
        if updated is None:
            raise HTTPException(404, "workflow not found")
        await audit.record(
            session, company_id, "agent", "workflow.edited",
            target=plan.name, metadata={"workflow_id": workflow_id, "instruction": instruction[:120]},
        )
        await session.commit()
    return {"applied": True, "workflow": _with_apps(profile, updated)}


@app.delete("/api/workflows/{workflow_id}")
async def delete_workflow_route(
    workflow_id: int, company_id: str = Depends(company_scope)
) -> dict:
    async with Session() as session:
        ok = await wf.delete_workflow(session, company_id, workflow_id)
        if not ok:
            raise HTTPException(404, "workflow not found")
        await audit.record(
            session, company_id, "ui", "workflow.deleted", metadata={"workflow_id": workflow_id}
        )
        await session.commit()
    return {"deleted": workflow_id}


@app.post("/api/workflows/{workflow_id}/run")
async def run_workflow_route(
    workflow_id: int,
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """Run a saved workflow now. External actions ride the same approval/dry-run
    brake as a click, plus the allowlist gate for anything not sanctioned to run
    unattended."""
    async with Session() as session:
        result = await run_workflow(session, profile, workflow_id, trigger="manual")
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@app.get("/api/workflows/{workflow_id}/runs")
async def list_workflow_runs_route(
    workflow_id: int, company_id: str = Depends(company_scope)
) -> dict:
    async with Session() as session:
        runs = await wf.list_runs(session, company_id, workflow_id)
    return {"runs": [r.model_dump(mode="json") for r in runs]}


@app.post("/api/norms/reset")
async def reset_norm_route(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    metric = str(payload.get("metric") or "")
    raw_date = payload.get("before_date")
    if not metric or not raw_date:
        raise HTTPException(400, "body must include metric and before_date")
    try:
        before_date = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, "before_date must be ISO-8601") from exc
    defn = next((r for r in profile.rhythms if r.get("name") == metric), None)
    async with Session() as session:
        baseline = await reset_norms(
            session, company_id, metric, before_date, reset_by=str(payload.get("by") or "agent"), defn=defn
        )
        version = await save_profile(session, profile, status="confirmed")
        await audit.record(
            session, company_id, str(payload.get("by") or "agent"), "norm.reset",
            target=metric, metadata={"before_date": before_date.isoformat(), "profile_version": version},
        )
        await session.commit()
    return {
        "metric": metric,
        "reset_before": before_date.isoformat(),
        "profile_version": version,
        "baseline": baseline.model_dump(mode="json") if baseline else None,
    }

@app.post("/api/situations/{situation_id}/propose")
async def propose(
    situation_id: str,
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """The Flags screen's Assign button: same candidates, same AI pick, same
    single-use links as the email brief."""
    async with Session() as session:
        situation = await get_situation(session, company_id, situation_id)
        if situation is None:
            raise HTTPException(404, "situation not found")
        roster = await team_roster(session, company_id)
        proposal = await propose_assignment(session, profile, situation, roster)
        await session.commit()  # the minted tokens must survive this request
    return {
        **{k: v for k, v in proposal.items() if k != "links"},
        "links": [link.model_dump() for link in proposal.get("links", [])],
    }


# ------------------------------- Act --------------------------------


@app.get("/api/actions")
async def actions(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    async with Session() as session:
        items = await list_actions(session, company_id)
        policy = await approval_policy(session, profile)
    return {"count": len(items), "actions": items, "dry_run": policy["dry_run"]}


@app.get("/api/actions/registry")
async def action_registry(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    async with Session() as session:
        policy = await approval_policy(session, profile)
    registry = profile.moves.get("registry", {}) or {}
    return {
        "actions": [
            {
                "name": k,
                "approval_required": v.get("approval_required", True),
                # `log` kinds never reach an external system — the UI must be
                # able to say so on the button rather than implying an effect
                "kind": v.get("kind"),
                "external_effect": v.get("kind") == "http",
                # so a card can filter out moves that cannot apply to the
                # record it is about, instead of offering a button whose only
                # possible outcome is a refusal
                "applies_to_url": v.get("applies_to_url"),
            }
            for k, v in registry.items()
        ],
        "policy": policy,
        "autonomy": profile.moves.get("autonomy", {}),
        # record types a reviewer targets — so the Work list shows a Review
        # button only where a reviewer actually exists (PRs), not on every issue
        "reviewable_types": reviewable_types(profile),
        # the words THIS business uses, so the UI stops saying "situation"
        # to a warehouse that calls it a supply risk
        "terms": profile.vocabulary.get("terms", {}) or {},
    }


@app.get("/api/settings")
async def get_settings(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    async with Session() as session:
        policy = await approval_policy(session, profile)
    return {"dry_run": policy["dry_run"], "autonomy": profile.moves.get("autonomy", {})}


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


# ------------------------------ Autonomy config ------------------------------


@app.get("/api/autonomy")
async def get_autonomy(
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """Everything the Autonomy screen needs: what the AI is allowed to do on its
    own, who it can assign to, and every move it COULD be allowed (so the UI
    shows real checkboxes, never invented ones)."""
    autonomy = profile.moves.get("autonomy", {}) or {}
    registry = profile.moves.get("registry", {}) or {}
    async with Session() as session:
        policy = await approval_policy(session, profile)
        roster = await team_roster(session, company_id)
    return {
        "live": not policy["dry_run"],
        "autonomy": {
            "enabled": autonomy.get("enabled", True),
            "allowed_actions": autonomy.get("allowed_actions", []),
            "min_confidence": autonomy.get("min_confidence", 0.7),
            "escalate_severities": autonomy.get("escalate_severities", []),
            "allow_public_actions": autonomy.get("allow_public_actions", False),
        },
        # every registered move, flagged so the UI can warn which ones post in
        # public — the same `public` fact the gate reads
        "available_actions": [
            {"name": k, "public": bool(v.get("public")), "kind": v.get("kind")}
            for k, v in registry.items()
        ],
        "roster": roster,
    }


@app.post("/api/autonomy")
async def update_autonomy(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """Update the autonomy policy — a new confirmed profile version (audited),
    the same way every other policy change is kept."""
    keys = ("enabled", "allowed_actions", "min_confidence", "escalate_severities", "allow_public_actions")
    async with Session() as session:
        updated = await set_autonomy(session, company_id, **{k: payload.get(k) for k in keys})
        await audit.record(session, company_id, "ui", "autonomy.updated", metadata=payload)
        await session.commit()
    if updated is None:
        raise HTTPException(400, "no valid autonomy fields to update")
    return {"autonomy": updated.moves.get("autonomy", {})}


async def _ensure_team_config(session: object, company_id: str) -> None:
    """Give a workspace a working assignment setup the first time it saves a
    roster, so auto-assign has a move to run and roles to notify — auto-detected
    from the connector's registry, never hardcoded to a tool."""
    profile = await load_profile(session, company_id)  # type: ignore[arg-type]
    if profile is None or (profile.moves.get("team", {}) or {}).get("assign_move"):
        return
    registry = profile.moves.get("registry", {}) or {}
    assign_move = next((n for n in registry if "assign" in n.lower()), None)
    if not assign_move:
        return
    team = {
        "assign_move": assign_move,
        "notify_roles": ["owner", "lead"],
        "roles": ["owner", "lead", "dev", "designer", "qa"],
        "workload": {},
    }
    await save_profile(session, profile.model_copy(update={"moves": {**profile.moves, "team": team}}))  # type: ignore[arg-type]


@app.get("/api/roster")
async def get_roster(company_id: str = Depends(company_scope)) -> dict:
    """The assignment roster — the people the AI may hand work to, with the
    skills the matcher reasons over. Distinct from workspace sign-in members."""
    async with Session() as session:
        roster = await team_roster(session, company_id)
    return {"roster": roster}


@app.post("/api/roster")
async def save_roster(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
) -> dict:
    """Replace the roster with the posted list. Light validation only — this is
    a small operator-curated table, not user input at scale."""
    roster = payload.get("roster")
    if not isinstance(roster, list):
        raise HTTPException(400, "body must be {\"roster\": [...]}")
    cleaned = []
    for m in roster:
        if not isinstance(m, dict) or not str(m.get("id", "")).strip():
            raise HTTPException(400, "each member needs an id (the login used to assign)")
        cleaned.append({
            "id": str(m["id"]).strip(),
            "name": str(m.get("name") or m["id"]).strip(),
            "email": str(m.get("email") or "").strip(),
            "roles": [str(r) for r in (m.get("roles") or []) if str(r).strip()],
            "skills": [str(s) for s in (m.get("skills") or []) if str(s).strip()],
            "max_open_issues": int(m.get("max_open_issues") or 3),
            "assignable": bool(m.get("assignable", True)),
        })
    async with Session() as session:
        await save_team_roster(session, company_id, cleaned)
        await _ensure_team_config(session, company_id)
        await audit.record(session, company_id, "ui", "roster.saved", metadata={"count": len(cleaned)})
        await session.commit()
    return {"roster": cleaned}


@app.post("/api/actions")
async def create_action(
    payload: dict = Body(...),
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    action = str(payload.get("action", ""))
    if action not in (profile.moves.get("registry") or {}):
        raise HTTPException(400, f"unknown action: {action}")
    async with Session() as session:
        result = await request_action(
            session,
            profile,
            action=action,
            params=payload.get("params", {}),
            situation_id=payload.get("situation_id"),
            requested_by=payload.get("requested_by", "ui"),
        )
    return result.model_dump()


@app.post("/api/reviews/{thing_id}/request-changes")
async def request_pr_review_route(
    thing_id: str,
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    """Assemble this PR's review findings into one inline REQUEST_CHANGES review
    and queue it for approval. Nothing is posted here — the move is public, so
    it lands in the approval queue and reaches GitHub only when a person
    approves it (and only for real once Practice mode is off)."""
    async with Session() as session:
        result = await request_pr_review(session, profile, thing_id, requested_by="ui")
        if result is None:
            raise HTTPException(404, "no open review findings for this record")
        await audit.record(
            session, company_id, "ui", "review.request_changes",
            target=thing_id, metadata={"action_id": result.id, "status": result.status},
        )
        await session.commit()
    return result.model_dump()


@app.post("/api/actions/{action_id}/{verdict}")
async def decide_action_route(
    action_id: int,
    verdict: str,
    company_id: str = Depends(company_scope),
    profile: Profile = Depends(profile_scope),
) -> dict:
    if verdict not in ("approve", "reject"):
        raise HTTPException(400, "verdict must be approve or reject")
    async with Session() as session:
        policy = await approval_policy(session, profile)
        result = await decide(
            session, action_id, verdict == "approve", "ui",
            profile.moves.get("registry", {}), policy,
        )
        # If that approval ran a CREATE, finish the cross-app chain — adopt the
        # new item's address and assign it — so approving a Slack incident's
        # "create issue" leaves it owned, exactly like the autonomous path.
        chained_assignee = None
        if verdict == "approve" and result.status == "executed":
            chained_assignee = await finish_create_chain(session, profile, action_id, result)
        await session.commit()
    return {**result.model_dump(), "chained_assignee": chained_assignee}


# --------------------------- Data deletion (CP6 part C) ---------------------------


@app.post("/api/company/delete")
async def request_company_deletion(
    payload: dict = Body(default={}),
    company_id: str = Depends(company_scope),
) -> dict:
    """Two-step erasure, deliberately not a single irreversible call.

    First call (no ``confirm``): creates a pending request, returns a
    confirmation token, deletes nothing. Second call, with
    ``{"confirm": true, "confirmation_token": "<the token>"}``: queues the
    real deletion as a worker job. Progress is tracked in deletion_requests,
    pollable via GET /api/company/delete.
    """
    confirm = bool(payload.get("confirm"))
    token = str(payload.get("confirmation_token") or "")
    requested_by = str(payload.get("requested_by") or "ui")

    async with Session() as session:
        if confirm:
            req = await confirm_deletion_request(session, company_id, token)
            if req is None:
                raise HTTPException(
                    400, "no pending deletion request matches that confirmation_token"
                )
            await audit.record(
                session, company_id, requested_by, "company.delete.confirmed",
                target=company_id, metadata={"request_id": req["id"]},
            )
            await session.commit()
        else:
            existing = await get_latest_deletion_request(session, company_id)
            if existing is not None and existing["status"] in ("pending_confirmation", "queued", "running"):
                req = existing
            else:
                req = await create_deletion_request(session, company_id, requested_by)
                await audit.record(
                    session, company_id, requested_by, "company.delete.requested",
                    target=company_id, metadata={"request_id": req["id"]},
                )
                await session.commit()
            if req["status"] == "pending_confirmation":
                return {
                    "status": "pending_confirmation",
                    "request_id": req["id"],
                    "confirmation_token": req["confirmation_token"],
                    "message": (
                        "This permanently erases ALL data for this company from Neo4j and "
                        "Postgres. Call POST /api/company/delete again with "
                        '{"confirm": true, "confirmation_token": "<this token>"} to proceed.'
                    ),
                }
            return {"status": req["status"], "request_id": req["id"]}

    pool = await create_pool(redis_settings())
    try:
        await pool.enqueue_job("delete_company_job", req["id"])
    finally:
        await pool.aclose()
    return {"status": "queued", "request_id": req["id"]}


@app.get("/api/company/delete")
async def company_deletion_status(company_id: str = Depends(company_scope)) -> dict:
    async with Session() as session:
        req = await get_latest_deletion_request(session, company_id)
    if req is None:
        raise HTTPException(404, "no deletion request for this company")
    return req


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




