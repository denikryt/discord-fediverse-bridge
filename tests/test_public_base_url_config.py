"""Behavior tests for deriving public endpoints from one configured base URL."""

from src.config import Settings


def _settings(**overrides: str) -> Settings:
    """Build settings with only the secrets required by the application contract."""
    values = {
        "DISCORD_TOKEN": "token",
        "FEDIFY_SHARED_SECRET": "secret",
        "PUBLIC_BASE_URL": "https://bridge.example.com/base/",
        "DISCORD_OAUTH_REDIRECT_URI": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_one_public_base_url_drives_all_public_endpoints() -> None:
    """Federation, registration, and OAuth must resolve from one operator value."""
    settings = _settings()

    assert settings.normalized_public_base_url == "https://bridge.example.com/base"
    assert settings.normalized_fedify_origin == "https://bridge.example.com/base"
    assert settings.normalized_public_bridge_base_url == "https://bridge.example.com/base"
    assert settings.resolved_discord_oauth_redirect_uri == (
        "https://bridge.example.com/base/auth/discord/callback"
    )


def test_explicit_oauth_redirect_remains_available_for_advanced_deployments() -> None:
    """An explicit callback override must remain possible without duplicating defaults."""
    settings = _settings(
        DISCORD_OAUTH_REDIRECT_URI="https://oauth.example.net/custom/callback"
    )

    assert settings.resolved_discord_oauth_redirect_uri == (
        "https://oauth.example.net/custom/callback"
    )
