from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

# Generic OAuth2 authorization-code flow. Like every other core module, the
# provider arrives as DATA (an OAuthProvider built by the connector), so this
# file knows nothing about GitHub, Slack or anyone else — it only knows the
# shape of the spec.
#
# Two halves of the dance:
#   1. authorize_url()  -> where we send the human to say yes. Public. Carries
#      `state`, which the caller must make unguessable AND single-use: it is
#      the only thing standing between this flow and a CSRF that attaches an
#      attacker's account to someone else's company.
#   2. exchange_code()  -> a BACK-CHANNEL POST, server to server, carrying the
#      client secret. The secret must never reach the browser, a redirect, a
#      log line or an error message — so nothing here ever echoes it back.


@dataclass(frozen=True)
class OAuthProvider:
    """Everything the flow needs for one provider."""

    name: str
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str
    scope: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


class OAuthError(Exception):
    """The provider refused, or answered with something unusable."""


def authorize_url(provider: OAuthProvider, state: str, redirect_uri: str) -> str:
    """The URL to send the user to. Contains only public values + state."""
    query = urlencode(
        {
            "client_id": provider.client_id,
            "redirect_uri": redirect_uri,
            "scope": provider.scope,
            "state": state,
            "response_type": "code",
        }
    )
    return f"{provider.authorize_url}?{query}"


async def exchange_code(provider: OAuthProvider, code: str, redirect_uri: str) -> str:
    """Trade the one-time code for an access token (back channel).

    Raises OAuthError with a provider-supplied reason. The message is built
    from the provider's error fields only — never from our credentials.
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.post(
                provider.token_url,
                data={
                    "client_id": provider.client_id,
                    "client_secret": provider.client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OAuthError(f"could not reach {provider.name}: {exc}") from exc

    if resp.status_code >= 400:
        raise OAuthError(f"{provider.name} rejected the code exchange (HTTP {resp.status_code})")
    try:
        body = resp.json()
    except ValueError as exc:
        raise OAuthError(f"{provider.name} returned a non-JSON token response") from exc

    # An OAuth2 provider signals failure INSIDE a 200 body, so status is not enough.
    if body.get("error"):
        raise OAuthError(
            f"{provider.name}: {body.get('error_description') or body['error']}"
        )
    token = body.get("access_token")
    if not token:
        raise OAuthError(f"{provider.name} returned no access_token")
    return str(token)
