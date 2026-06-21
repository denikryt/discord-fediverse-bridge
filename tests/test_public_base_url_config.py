"""Behavior tests for deriving public endpoints from one configured origin."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings


def _settings(**overrides: object) -> Settings:
    """Build settings with only the secrets required by the application contract."""
    values: dict[str, object] = {
        "DISCORD_TOKEN": "token",
        "FEDIFY_SHARED_SECRET": "secret",
        "BRIDGE_SUPER_ADMIN_USER_IDS": "1",
        "PUBLIC_BASE_URL": "https://bridge.example.com/",
        "DISCORD_OAUTH_REDIRECT_URI": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_one_public_origin_drives_all_public_endpoints() -> None:
    """Federation, registration, and derived OAuth URLs resolve from one origin."""
    settings = _settings()

    assert settings.normalized_public_base_url == "https://bridge.example.com"
    assert settings.normalized_fedify_origin == "https://bridge.example.com"
    assert settings.normalized_public_bridge_base_url == "https://bridge.example.com"
    assert settings.resolved_discord_oauth_redirect_uri == (
        "https://bridge.example.com/auth/discord/callback"
    )


def test_public_origin_allows_explicit_port() -> None:
    """A non-default public port is part of the supported ActivityPub origin."""
    settings = _settings(PUBLIC_BASE_URL="https://bridge.example.com:8443/")

    assert settings.normalized_public_base_url == "https://bridge.example.com:8443"
    assert settings.resolved_discord_oauth_redirect_uri == (
        "https://bridge.example.com:8443/auth/discord/callback"
    )


@pytest.mark.parametrize(
    "public_base_url",
    [
        "https://bridge.example.com/base",
        "https://bridge.example.com/app/",
        "https://bridge.example.com?x=1",
        "https://bridge.example.com#x",
        "https://user:pass@bridge.example.com",
        "/bridge",
    ],
)
def test_public_base_url_must_be_origin_only(public_base_url: str) -> None:
    """Unsupported path-mounted or non-origin public bases fail at config load."""
    with pytest.raises(ValidationError):
        _settings(PUBLIC_BASE_URL=public_base_url)


def test_explicit_oauth_redirect_remains_available_for_advanced_deployments() -> None:
    """An explicit pathful OAuth callback override remains independent of AP origin."""
    settings = _settings(
        DISCORD_OAUTH_REDIRECT_URI="https://oauth.example.net/custom/callback"
    )

    assert settings.resolved_discord_oauth_redirect_uri == (
        "https://oauth.example.net/custom/callback"
    )
