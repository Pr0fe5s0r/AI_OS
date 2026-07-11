#!/usr/bin/env python3
"""Send one test email through the real delivery path.

Verifies SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD end to end, using the
same code that delivers a real situation brief — including the HTML buttons.

    docker compose exec -T api python scripts/send_test_email.py you@example.com

Prints the delivery status. `sent` means it really left the machine.
"""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime

from packages.core.deliver import deliver
from packages.shared.schema import ActionLink, Brief, Evidence


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    recipients = sys.argv[1:]

    host = os.getenv("SMTP_HOST")
    if not host:
        print("SMTP_HOST is empty -> nothing would be sent. Set it in .env.")
        return 1
    if host != "mailpit" and not os.getenv("SMTP_PASSWORD"):
        print(f"SMTP_PASSWORD is empty -> {host} will reject the login.")
        print("Put your app password on the SMTP_PASSWORD line in .env, then:")
        print("  docker compose up -d --force-recreate api worker")
        return 1

    brief = Brief(
        situation_id="test:smtp",
        title="AI OS test email",
        severity="medium",
        summary="If you can read this, SMTP is configured correctly.",
        recommended_action="Nothing to do — this is a connectivity test.",
        assigned_to="nobody",
        citations=["test:smtp"],
        evidence=[Evidence(event_id="test", source="github",
                           timestamp=datetime.now(UTC), excerpt="test")],
        action_links=[ActionLink(label="Example button", url="https://example.com", primary=True)],
    )
    routing = {
        "from": os.getenv("SMTP_FROM", "ai-os@localhost"),
        "dry_run": False,
        "routes": {"medium": {"channel": "email", "recipients": recipients}},
        "default": {"channel": "console", "recipients": recipients},
    }

    receipt = deliver(brief, routing)
    print(f"\nchannel={receipt.channel} recipient={receipt.recipient} status={receipt.status}")
    if receipt.status == "sent":
        print("OK - check the inbox.")
        return 0
    print("NOT sent. Fix the reason above and re-run.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
