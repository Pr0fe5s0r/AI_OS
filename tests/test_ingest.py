from __future__ import annotations

from apps.common.context import build_source_config
from packages.connectors.github import GitHubConnector
from packages.core.ingest import ingest
from packages.shared.schema import Event
from tests.conftest import INVENTORY_PROFILE, SOFTWARE_PROFILE

# Fixtures live here (not in the app) — real GitHub REST payload shapes, so CI
# runs offline. The mapping under test comes from the PROFILE, not from code.

_GITHUB_DEF = next(s for s in SOFTWARE_PROFILE.sources if s["source"] == "github")
_SOURCE_CONFIG = build_source_config(SOFTWARE_PROFILE, _GITHUB_DEF, {"repo": "acme/web-store"})

_ISSUE = {
    "number": 4821,
    "title": "Checkout returns 500 at payment step",
    "body": "Stripe charge created but order never marked paid.",
    "state": "open",
    "user": {"login": "maya", "id": 7},
    "labels": [{"name": "bug"}, {"name": "payments"}],
    "html_url": "https://github.com/acme/web-store/issues/4821",
    "created_at": "2026-07-09T14:22:00Z",
    "closed_at": None,
    "_repo": "web-store",
    "_merged_at": None,
}

_PR = {
    "number": 4830,
    "title": "Fix checkout 500",
    "body": "Wrap finalize in a transaction. Closes #4821.",
    "state": "closed",
    "user": {"login": "maya", "id": 7},
    "labels": [{"name": "fix"}],
    "html_url": "https://github.com/acme/web-store/pull/4830",
    "created_at": "2026-07-10T09:12:00Z",
    "closed_at": "2026-07-10T14:12:00Z",
    "pull_request": {"url": "..."},
    "_repo": "web-store",
    "_merged_at": "2026-07-10T14:12:00Z",
}


def test_ingest_normalizes_issue_into_event_schema() -> None:
    ev = ingest(_SOURCE_CONFIG, _ISSUE)
    assert isinstance(ev, Event)
    assert ev.id == "gh-web-store-4821"
    assert ev.source == "github"
    assert ev.type == "issue"
    assert ev.actor.name == "maya"
    assert "Checkout returns 500" in ev.content
    assert ev.metadata["number"] == 4821
    assert ev.metadata["state"] == "open"
    # the untouched source payload is preserved
    assert ev.raw["html_url"].endswith("/issues/4821")


def test_ingest_detects_pull_request_and_merge_time() -> None:
    ev = ingest(_SOURCE_CONFIG, _PR)
    assert ev.type == "pull_request"
    assert ev.id == "gh-web-store-4830"
    assert ev.metadata["merged_at"] == "2026-07-10T14:12:00Z"


def test_the_same_engine_ingests_a_purchase_order_under_the_other_profile() -> None:
    """Two-profile proof at the normalizer level: same ingest(), different data."""
    ops_def = next(s for s in INVENTORY_PROFILE.sources if s["source"] == "ops")
    config = build_source_config(INVENTORY_PROFILE, ops_def, {})
    ev = ingest(config, {
        "ref": "PO-9", "kind": "purchase_order", "status": "open",
        "occurred_at": "2026-07-01T08:00:00Z", "expected_at": "2026-07-08T08:00:00Z",
        "title": "PO-9 restock widgets", "notes": "500 units",
        "supplier": "Vendaco", "sku": "SKU-1", "quantity": 500,
        "actor": {"id": "vendaco", "name": "Vendaco"},
    })
    assert ev.id == "ops-PO-9"
    assert ev.company_id == "acme-inventory"
    assert ev.type == "purchase_order"
    assert ev.metadata["supplier"] == "Vendaco"


def test_connector_is_token_aware(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert GitHubConnector("acme/web-store", token=None).authenticated is False
    assert GitHubConnector("acme/web-store", token="ghp_x").authenticated is True
