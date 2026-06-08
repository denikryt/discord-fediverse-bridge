"""Behavior-focused coverage for dynamic bridge policy foundations."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.bridge_policy import BridgePolicyService, FederationPolicyReason, PolicyType
from src.config import Settings
from src.db import Database


def _settings_data(**overrides: str) -> dict[str, str]:
    """Return the minimum valid environment-shaped settings payload."""
    data = {
        "DISCORD_TOKEN": "token",
        "PUBLIC_BASE_URL": "https://bridge.example",
        "FEDIFY_SHARED_SECRET": "secret",
        "BRIDGE_SUPER_ADMIN_USER_IDS": "100",
    }
    data.update(overrides)
    return data


def test_settings_require_bootstrap_super_admin() -> None:
    """Startup validation rejects a deployment with no bootstrap administrator."""
    with pytest.raises(ValidationError):
        Settings.model_validate(_settings_data(BRIDGE_SUPER_ADMIN_USER_IDS=""))


def test_dynamic_blocklist_overrides_bootstrap_allowlist(tmp_path: Path) -> None:
    """A committed dynamic block immediately overrides an immutable bootstrap allow."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.migrate()
    settings = Settings.model_validate(
        _settings_data(FEDERATION_ALLOWLIST="remote.example")
    )
    database.bridge_policy_entries.create_active(
        policy_type=PolicyType.FEDERATION_BLOCK.value,
        normalized_subject="remote.example",
        actor_discord_user_id="100",
        reason="blocked",
    )

    decision = BridgePolicyService(settings=settings, repository=database.bridge_policy_entries).snapshot().federation_decision(
        "https://remote.example/c/test"
    )

    assert decision.allowed is False
    assert decision.reason is FederationPolicyReason.BLOCKLISTED


def test_empty_allowlists_are_unrestricted_but_blocklists_still_deny(tmp_path: Path) -> None:
    """Empty allowlists preserve open mode while explicit blocks remain effective."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    settings = Settings.model_validate(_settings_data())
    database.bridge_policy_entries.create_active(
        policy_type=PolicyType.DISCORD_GUILD_BLOCK.value,
        normalized_subject="200",
        actor_discord_user_id="100",
        reason=None,
    )
    snapshot = BridgePolicyService(settings=settings, repository=database.bridge_policy_entries).snapshot()

    assert snapshot.is_discord_guild_allowed("199") is True
    assert snapshot.is_discord_guild_allowed("200") is False
    assert snapshot.federation_decision("open.example").allowed is True
