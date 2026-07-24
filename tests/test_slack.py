from __future__ import annotations

from packages.connectors.base import oauth_provider_for
from packages.connectors.slack import SlackConnector

# The Slack connector is real transport, not a stub: it names authors and filters
# system noise before a message reaches the engine.


def test_enrich_names_authors_and_drops_system_messages():
    c = SlackConnector(token="x", channel="support")
    names = {"U1": "Priya Rao", "U2": "Sam Lee"}
    raw = [
        {"ts": "1", "text": "the chatbot shows an error", "user": "U1"},
        {"ts": "2", "text": "joined", "user": "U2", "subtype": "channel_join"},  # noise
        {"ts": "3", "text": "the create-account button is broken", "user": "U2"},
        {"ts": "4", "text": "from a bot", "user": "U9"},  # unknown user -> id kept
    ]
    out = c._enrich(raw, "C123", names)
    assert [m["ts"] for m in out] == ["1", "3", "4"], "system messages must be dropped"
    assert out[0]["_user_name"] == "Priya Rao"
    assert out[1]["_user_name"] == "Sam Lee"
    assert out[2]["_user_name"] == "U9"  # can't name it -> keep the id, don't invent
    assert all(m["_channel"] == "support" for m in out)


def test_slack_declares_an_oauth_provider():
    p = oauth_provider_for("slack")
    assert p is not None and p.name == "slack"
    assert "channels:history" in p.scope and "users:read" in p.scope


def test_ingest_parses_slack_epoch_timestamps():
    # Slack's `ts` is a Unix epoch ("1784921347.508139"), not ISO — it used to
    # blow up ingest with "Invalid isoformat string" and silently drop the message.
    from packages.core.ingest import _parse_ts
    dt = _parse_ts("1784921347.508139")
    assert dt.tzinfo is not None and dt.year >= 2026
    assert _parse_ts(1784921347).year >= 2026          # numeric epoch
    assert _parse_ts("2026-07-25T10:00:00Z").year == 2026  # ISO still works
