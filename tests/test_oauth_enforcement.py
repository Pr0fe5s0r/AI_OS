from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from packages.core.tenancy import company_scope

# Where a real sign-in exists, it is the only way in.
#
# A pasted token is a long-lived secret with whatever breadth the person
# happened to grant it; an OAuth grant is scoped, attributable, and revocable
# from the provider's own settings page. Hiding the token field in the UI does
# not enforce that — the endpoint still writes a credential — so the refusal is
# pinned at the API.

COMPANY = "test-oauth-enforcement"


@pytest.fixture
def client(monkeypatch):
    app.dependency_overrides[company_scope] = lambda: COMPANY
    yield TestClient(app)
    app.dependency_overrides.pop(company_scope, None)


def test_a_pasted_token_is_refused_when_sign_in_is_available(client, monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_ID", "id-123")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "secret-456")

    resp = client.post("/api/connections/github", json={"token": "ghp_pasted", "repo": "a/b"})

    assert resp.status_code == 400
    assert "signing in" in resp.json()["detail"]


def test_the_refusal_names_the_way_in_rather_than_just_saying_no(client, monkeypatch):
    """A 400 that doesn't say what to do instead reads as the app being broken."""
    monkeypatch.setenv("GITHUB_CLIENT_ID", "id-123")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "secret-456")

    detail = client.post(
        "/api/connections/github", json={"token": "ghp_pasted"}
    ).json()["detail"]

    assert "Sign in with Github" in detail
    assert "revocable" in detail


def test_a_source_with_no_oauth_app_configured_still_takes_a_token(client, monkeypatch):
    """Refusing here would mean nobody can connect that source at all — the
    enforcement is 'prefer the better door', not 'brick up the only one'."""
    monkeypatch.setenv("GITHUB_CLIENT_ID", "")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "")

    resp = client.post("/api/connections/github", json={"token": "ghp_pasted", "repo": "a/b"})

    assert resp.status_code != 400


def test_config_only_updates_are_untouched(client, monkeypatch):
    """Picking which repo to watch AFTER signing in sends no token. That must
    keep working, or an OAuth user can never choose a target."""
    monkeypatch.setenv("GITHUB_CLIENT_ID", "id-123")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "secret-456")

    resp = client.post("/api/connections/github", json={"repo": "a/b"})

    assert resp.status_code != 400
