"""Settings tests for Discord guild allowlist parsing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings


def _settings_data(**overrides: object) -> dict[str, object]:
    """Return the minimum data needed to construct Settings in tests."""
    data: dict[str, object] = {
        "DISCORD_TOKEN": "discord-token",
        "FEDIFY_SHARED_SECRET": "secret",
        "PUBLIC_BASE_URL": "https://bridge.example",
        "BRIDGE_SUPER_ADMIN_USER_IDS": "1",
    }
    data.update(overrides)
    return data


def test_discord_guild_allowlist_defaults_to_unrestricted() -> None:
    """Unset allowlists are represented as an empty list."""
    settings = Settings.model_validate(_settings_data())

    assert settings.discord_guild_allowlist == []


def test_discord_guild_allowlist_parses_comma_separated_ids() -> None:
    """Comma-separated env values are trimmed and kept as strings."""
    settings = Settings.model_validate(
        _settings_data(DISCORD_GUILD_ALLOWLIST="123456789012345678, 987654321098765432")
    )

    assert settings.discord_guild_allowlist == ["123456789012345678", "987654321098765432"]


def test_discord_guild_allowlist_empty_string_means_unrestricted() -> None:
    """An explicitly empty env value is equivalent to an unset value."""
    settings = Settings.model_validate(_settings_data(DISCORD_GUILD_ALLOWLIST=""))

    assert settings.discord_guild_allowlist == []


def test_discord_guild_allowlist_rejects_non_decimal_entries() -> None:
    """Malformed guild ids fail settings construction at startup."""
    with pytest.raises(ValidationError):
        Settings.model_validate(_settings_data(DISCORD_GUILD_ALLOWLIST="123,not-a-guild"))
