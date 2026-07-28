from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import text

from packages.core.db import Session
from packages.core.oauth import OAuthError, OAuthProvider, authorize_url, exchange_code
from packages.core.tokens import consume_token, issue_token

pytestmark = pytest.mark.needs_db

_CO = "test-oauth"
_PURPOSE = "oauth_state"

_PROVIDER = OAuthProvider(
    name="github",
    authorize_url="https://github.com/login/oauth/authorize",
    token_url="https://github.com/login/oauth/access_token",
    client_id="cid-123",
    client_secret="super-secret",
    scope="public_repo",
)


# ------------------------------- authorize_url -------------------------------


def test_authorize_url_carries_only_public_values() -> None:
    url = authorize_url(_PROVIDER, state="st-1", redirect_uri="http://localhost:8000/cb")
    query = parse_qs(urlparse(url).query)

    assert url.startswith("https://github.com/login/oauth/authorize?")
    assert query["client_id"] == ["cid-123"]
    assert query["state"] == ["st-1"]
    assert query["scope"] == ["public_repo"]
    assert query["redirect_uri"] == ["http://localhost:8000/cb"]
    # the secret must never travel through the browser
    assert "super-secret" not in url


def test_authorize_url_encodes_state_safely() -> None:
    """State is a sealed blob; it must survive the round trip intact."""
    state = "abc+def/ghi=="
    url = authorize_url(_PROVIDER, state=state, redirect_uri="http://localhost:8000/cb")
    assert parse_qs(urlparse(url).query)["state"] == [state]


def test_provider_is_not_configured_without_credentials() -> None:
    bare = OAuthProvider(
        name="github", authorize_url="a", token_url="b", client_id="", client_secret="", scope="s"
    )
    assert bare.configured is False
    assert _PROVIDER.configured is True


# --------------------------- state: CSRF + replay ---------------------------
# The OAuth `state` reuses the same single-use token machinery the one-click
# email links use. These tests pin the two properties that make it a real CSRF
# defence rather than decoration.


async def _reset() -> None:
    async with Session() as session:
        await session.execute(text("DELETE FROM action_tokens WHERE company_id = :c"), {"c": _CO})
        await session.commit()


async def test_state_round_trips_and_carries_the_company() -> None:
    await _reset()
    async with Session() as session:
        state = await issue_token(session, _CO, _PURPOSE, {"source": "github"}, ttl_minutes=10)
        await session.commit()

        body = await consume_token(session, state, _PURPOSE, used_by="oauth-callback")
        await session.commit()

    assert body is not None
    # the callback learns the company from a value WE minted, not from the URL
    assert body["company_id"] == _CO
    assert body["source"] == "github"


async def test_a_replayed_state_is_rejected() -> None:
    """Single-use: a callback URL captured from logs or history cannot be
    fired a second time to re-attach a token."""
    await _reset()
    async with Session() as session:
        state = await issue_token(session, _CO, _PURPOSE, {"source": "github"}, ttl_minutes=10)
        await session.commit()

        first = await consume_token(session, state, _PURPOSE)
        await session.commit()
        second = await consume_token(session, state, _PURPOSE)
        await session.commit()

    assert first is not None
    assert second is None, "a state token must never be usable twice"


async def test_a_forged_state_is_rejected() -> None:
    await _reset()
    async with Session() as session:
        assert await consume_token(session, "not-a-real-token", _PURPOSE) is None


async def test_an_expired_state_is_rejected() -> None:
    await _reset()
    async with Session() as session:
        state = await issue_token(session, _CO, _PURPOSE, {"source": "github"}, ttl_minutes=10)
        # wind the clock past its life
        await session.execute(
            text("UPDATE action_tokens SET expires_at = now() - interval '1 minute' WHERE company_id = :c"),
            {"c": _CO},
        )
        await session.commit()

        assert await consume_token(session, state, _PURPOSE) is None


async def test_state_for_one_purpose_cannot_be_used_for_another() -> None:
    """An OAuth state must not double as an email-approval capability."""
    await _reset()
    async with Session() as session:
        state = await issue_token(session, _CO, _PURPOSE, {"source": "github"}, ttl_minutes=10)
        await session.commit()

        assert await consume_token(session, state, "assign") is None


# ------------------------------ code exchange ------------------------------


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    """Stubs only the HTTP boundary; the flow logic under test stays real."""

    def __init__(self, response) -> None:
        self._response = response
        self.sent: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, headers=None):
        self.sent = {"url": url, "data": data, "headers": headers}
        return self._response


def _patch_client(monkeypatch, response) -> _FakeClient:
    client = _FakeClient(response)
    monkeypatch.setattr("packages.core.oauth.httpx.AsyncClient", lambda **kw: client)
    return client


async def test_exchange_code_returns_the_token_and_sends_the_secret_backchannel(monkeypatch) -> None:
    client = _patch_client(monkeypatch, _FakeResponse({"access_token": "gho_real", "scope": "public_repo"}))

    token = await exchange_code(_PROVIDER, "the-code", "http://localhost:8000/cb")

    assert token == "gho_real"
    # the secret goes in the POST body, server-to-server — never a URL
    assert client.sent["data"]["client_secret"] == "super-secret"
    assert client.sent["data"]["code"] == "the-code"
    assert client.sent["headers"]["Accept"] == "application/json"


async def test_exchange_code_surfaces_a_provider_error_inside_a_200(monkeypatch) -> None:
    """OAuth2 reports failure in the BODY with a 200 — status alone is a trap."""
    _patch_client(
        monkeypatch,
        _FakeResponse({"error": "bad_verification_code", "error_description": "The code is incorrect."}),
    )

    with pytest.raises(OAuthError) as exc:
        await exchange_code(_PROVIDER, "stale", "http://localhost:8000/cb")
    assert "The code is incorrect." in str(exc.value)


async def test_exchange_code_rejects_a_response_with_no_token(monkeypatch) -> None:
    _patch_client(monkeypatch, _FakeResponse({"scope": "public_repo"}))

    with pytest.raises(OAuthError) as exc:
        await exchange_code(_PROVIDER, "code", "http://localhost:8000/cb")
    assert "no access_token" in str(exc.value)


async def test_exchange_code_never_leaks_the_secret_in_an_error(monkeypatch) -> None:
    _patch_client(monkeypatch, _FakeResponse({"error": "server_error"}, status_code=500))

    with pytest.raises(OAuthError) as exc:
        await exchange_code(_PROVIDER, "code", "http://localhost:8000/cb")
    assert "super-secret" not in str(exc.value)


# ------------------------ the token survives a config edit ------------------------


async def test_saving_config_without_a_token_keeps_the_sealed_one() -> None:
    """After an OAuth grant the UI saves which repo to watch, sending no
    token. That must not wipe the token we just went through the whole flow
    to obtain — only DELETE clears a credential."""
    from packages.core.credentials import get_credential, save_credential

    async with Session() as session:
        await session.execute(text("DELETE FROM credentials WHERE company_id = :c"), {"c": _CO})
        await save_credential(session, _CO, "github", "gho_from_oauth", {})
        await session.commit()

        # what the connect endpoint does when the body carries no token
        current = await get_credential(session, _CO, "github")
        token = current[0] if current else ""
        await save_credential(session, _CO, "github", token, {"repo": "acme/web"})
        await session.commit()

        after = await get_credential(session, _CO, "github")

    assert after is not None
    assert after[0] == "gho_from_oauth", "the OAuth token must survive a config-only save"
    assert after[1]["repo"] == "acme/web"
