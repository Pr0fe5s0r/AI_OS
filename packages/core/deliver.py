from __future__ import annotations

import html
import logging
import os
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage

from packages.shared.schema import Brief, DeliveryReceipt

log = logging.getLogger("aios.deliver")

# Generic routing + delivery. routing_config (vertical data) maps severity ->
# {channel, recipients}. Channels are generic transports; the vertical decides
# who gets what.
#
# routing_config = {
#   "from": "ai-os@company.com",
#   "routes": {"high": {"channel": "email", "recipients": ["pm@x.com"]}},
#   "default": {"channel": "console", "recipients": []},
#   "dry_run": True,          # same switch as actions: compose it, don't send it
# }


def _route(severity: str, routing_config: dict) -> tuple[str, list[str]]:
    entry = routing_config.get("routes", {}).get(severity) or routing_config.get("default", {})
    channel = entry.get("channel", "console")
    recipients = list(entry.get("recipients") or [])
    if channel == "email" and not recipients:
        channel = "console"  # nobody to mail — never silently drop the brief
    return channel, recipients


def _render(brief: Brief) -> tuple[str, str]:
    # The [ref:…] tag rides in the subject so a reply (which quotes the subject)
    # can be matched back to this situation — see apps.common.inbound.
    tag = f" [ref:{brief.reply_ref}]" if brief.reply_ref else ""
    subject = f"[{brief.severity.upper()}] {brief.title}{tag}"
    lines = [
        f"Severity : {brief.severity.upper()}",
        f"Situation: {brief.title}",
        f"Owner    : {brief.assigned_to or 'UNASSIGNED'}",
        "",
        "Description",
        "-----------",
        brief.summary or "(none)",
    ]
    if brief.recommended_action:
        lines += ["", "Recommended action", "------------------", brief.recommended_action]
    if brief.note:
        lines += ["", brief.note]
    if brief.action_links:
        lines += ["", "Assign this work", "----------------"]
        lines += [f"  {link.label}\n    {link.url}" for link in brief.action_links]
    if brief.citations:
        lines += ["", "Evidence", "--------", *[f"  - {c}" for c in brief.citations]]
    lines += ["", f"situation_id: {brief.situation_id}", "-- AI OS"]
    return subject, "\n".join(lines)


_SEV_COLOR = {"critical": "#d1242f", "high": "#bf8700", "medium": "#0969da", "low": "#57606a"}


def _render_html(brief: Brief) -> str:
    color = _SEV_COLOR.get(brief.severity, "#57606a")
    esc = html.escape

    buttons = ""
    for link in brief.action_links:
        bg = "#1f883d" if link.primary else "#ffffff"
        fg = "#ffffff" if link.primary else "#24292f"
        buttons += (
            f'<a href="{esc(link.url)}" style="display:inline-block;margin:4px 8px 4px 0;'
            f"padding:10px 16px;border:1px solid #d0d7de;border-radius:6px;background:{bg};"
            f'color:{fg};text-decoration:none;font-weight:600;font-size:14px">{esc(link.label)}</a>'
        )

    evidence = "".join(f"<li style='margin:2px 0'>{esc(c)}</li>" for c in brief.citations)
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:640px;color:#24292f">
  <div style="border-left:4px solid {color};padding-left:12px;margin-bottom:16px">
    <div style="font-size:12px;text-transform:uppercase;letter-spacing:.5px;color:{color};font-weight:700">
      {esc(brief.severity)}
    </div>
    <h2 style="margin:4px 0 0;font-size:20px">{esc(brief.title)}</h2>
    <div style="color:#57606a;font-size:13px;margin-top:4px">
      Owner: <b>{esc(brief.assigned_to or "UNASSIGNED")}</b>
    </div>
  </div>
  <p style="font-size:14px;line-height:1.6">{esc(brief.summary or "")}</p>
  {f'<p style="font-size:14px;line-height:1.6"><b>Recommended:</b> {esc(brief.recommended_action)}</p>' if brief.recommended_action else ""}
  {f'<p style="font-size:13px;color:#57606a">{esc(brief.note)}</p>' if brief.note else ""}
  {f'<div style="margin:18px 0">{buttons}</div>' if buttons else ""}
  {f'<div style="font-size:12px;color:#57606a"><b>Evidence</b><ul style="padding-left:18px">{evidence}</ul></div>' if evidence else ""}
  <hr style="border:none;border-top:1px solid #d0d7de;margin:18px 0">
  <div style="font-size:11px;color:#8c959f">situation_id: {esc(brief.situation_id)} &middot; sent by AI OS</div>
</div>"""


def _print(subject: str, body: str, recipients: list[str], prefix: str) -> None:
    banner = "=" * 72
    text = f"\n{banner}\n{prefix} -> {', '.join(recipients) or 'console'}\n{subject}\n{'-' * 72}\n{body}\n{banner}"
    log.warning(text)
    print(text, flush=True)


def _send_console(brief: Brief, recipients: list[str], routing_config: dict) -> str:
    subject, body = _render(brief)
    _print(subject, body, recipients, "CONSOLE")
    return "sent"


def _send_email(brief: Brief, recipients: list[str], routing_config: dict) -> str:
    subject, body = _render(brief)
    dry_run = bool(routing_config.get("dry_run", True))

    if dry_run:
        _print(subject, body, recipients, "EMAIL (dry_run — NOT sent)")
        return "dry_run"

    host = os.getenv("SMTP_HOST")
    if not host:
        _print(subject, body, recipients, "EMAIL (SMTP not configured — NOT sent)")
        return "email_not_configured"

    message = EmailMessage()
    message["From"] = routing_config.get("from", os.getenv("SMTP_FROM", "ai-os@localhost"))
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)
    message.add_alternative(_render_html(brief), subtype="html")

    port = int(os.getenv("SMTP_PORT", "587"))
    user, password = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD")
    try:
        # 465 = implicit TLS. Otherwise upgrade with STARTTLS *only if the server
        # offers it* — local relays (Mailpit, MailHog) and some corporate ones
        # do not, and demanding it makes every send fail.
        smtp = (
            smtplib.SMTP_SSL(host, port, timeout=20)
            if port == 465
            else smtplib.SMTP(host, port, timeout=20)
        )
        with smtp:
            smtp.ehlo()
            if port != 465 and smtp.has_extn("starttls"):
                smtp.starttls()
                smtp.ehlo()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(message)
    except Exception as exc:  # a failed notification must not kill the run
        log.error("email delivery failed: %s", exc)
        return f"email_failed: {exc}"

    _print(subject, body, recipients, "EMAIL (sent)")
    return "sent"


_CHANNELS = {"console": _send_console, "email": _send_email}


def deliver(brief: Brief, routing_config: dict) -> DeliveryReceipt:
    channel, recipients = _route(brief.severity, routing_config)
    handler = _CHANNELS.get(channel)
    status = handler(brief, recipients, routing_config) if handler else f"unsupported_channel:{channel}"
    return DeliveryReceipt(
        situation_id=brief.situation_id,
        channel=channel,
        recipient=", ".join(recipients) or "console",
        status=status,
        delivered_at=datetime.now(UTC),
    )
