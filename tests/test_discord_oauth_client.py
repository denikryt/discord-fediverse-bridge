"""Boundary tests for the Discord OAuth HTTP client."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from src.discord_oauth_client import DiscordOAuthClient


class _FakeResponse:
    """Provide the small subset of httpx.Response behavior the client uses."""

    def __init__(
        self,
        *,
        status_code: int,
        payload: dict[str, object] | None = None,
        text: str = "",
        request: httpx.Request | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.reason_phrase = "error"
        self.request = request or httpx.Request(
            "POST",
            "https://discord.com/api/oauth2/token",
        )

    def raise_for_status(self) -> None:
        """Mirror httpx behavior for non-success responses."""
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom",
                request=self.request,
                response=self,  # type: ignore[arg-type]
            )

    def json(self) -> dict[str, object]:
        """Return the fake JSON payload for success-path assertions."""
        return self._payload


class _RecordingAsyncClient:
    """Record outbound token exchange parameters and return a fake response."""

    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.last_post_kwargs: dict[str, object] | None = None

    async def post(self, url: str, **kwargs: object) -> _FakeResponse:
        """Record the request shape for later assertions."""
        self.last_post_kwargs = {"url": url, **kwargs}
        return self.response

    async def aclose(self) -> None:
        """Match the real AsyncClient shutdown API."""
        return None


@pytest.mark.asyncio
async def test_exchange_code_uses_basic_auth_and_documented_form_body() -> None:
    """Token exchange should follow Discord's documented HTTP Basic auth contract."""
    settings = SimpleNamespace(
        discord_oauth_client_id="client-id",
        discord_oauth_client_secret="client-secret",
        resolved_discord_oauth_redirect_uri="https://bridge.example.com/auth/discord/callback",
    )
    client = DiscordOAuthClient(settings)
    fake_http = _RecordingAsyncClient(
        _FakeResponse(status_code=200, payload={"access_token": "token"})
    )
    client._client = fake_http  # type: ignore[assignment]

    access_token = await client.exchange_code_for_access_token("oauth-code")

    assert access_token == "token"
    assert fake_http.last_post_kwargs is not None
    assert fake_http.last_post_kwargs["url"] == "https://discord.com/api/oauth2/token"
    assert fake_http.last_post_kwargs["auth"] == ("client-id", "client-secret")
    assert fake_http.last_post_kwargs["data"] == {
        "grant_type": "authorization_code",
        "code": "oauth-code",
        "redirect_uri": "https://bridge.example.com/auth/discord/callback",
    }


@pytest.mark.asyncio
async def test_exchange_code_surfaces_discord_error_body() -> None:
    """HTTP failures should include Discord's response body for diagnosis."""
    settings = SimpleNamespace(
        discord_oauth_client_id="client-id",
        discord_oauth_client_secret="client-secret",
        resolved_discord_oauth_redirect_uri="https://bridge.example.com/auth/discord/callback",
    )
    client = DiscordOAuthClient(settings)
    fake_http = _RecordingAsyncClient(
        _FakeResponse(
            status_code=500,
            text='{"message":"Internal Server Error","error":500}',
        )
    )
    client._client = fake_http  # type: ignore[assignment]

    with pytest.raises(RuntimeError) as excinfo:
        await client.exchange_code_for_access_token("oauth-code")

    assert "Discord token exchange failed: 500" in str(excinfo.value)
    assert "Internal Server Error" in str(excinfo.value)


def test_oauth_client_requires_resolved_redirect_uri_contract() -> None:
    """An old settings adapter without the resolved property fails immediately."""
    settings = SimpleNamespace(
        discord_oauth_client_id="client-id",
        discord_oauth_client_secret="client-secret",
        discord_oauth_redirect_uri="https://legacy.example/callback",
    )
    client = DiscordOAuthClient(settings)

    with pytest.raises(AttributeError):
        client.build_authorization_url("state")
