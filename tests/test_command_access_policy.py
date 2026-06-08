"""Behavior tests for reusable Discord command-access policies."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from discordops import evaluate_policy

from src.bridge_policy import BridgePolicySnapshot, EffectivePolicyEntry, PolicyType
from src.operations.common_preconditions import (
    GUILD_COMMAND_ACCESS,
    MANAGE_GUILD_COMMAND_ACCESS,
    REGISTERED_GUILD_COMMAND_ACCESS,
    REGISTRATION_REQUIRED_MESSAGE,
    CommandAccessInput,
)
from src.user_bans import BanDecision


class _PolicyService:
    def __init__(self, snapshot: BridgePolicySnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> BridgePolicySnapshot:
        return self._snapshot


class _BanService:
    def __init__(self, decision: BanDecision | None = None) -> None:
        self.decision = decision or BanDecision(False)

    def check_global_discord_user(self, discord_user_id: str) -> BanDecision:
        return self.decision


def _snapshot(
    *,
    allowed_guilds: tuple[str, ...] = (),
    blocked_guilds: tuple[str, ...] = (),
) -> BridgePolicySnapshot:
    entries = [
        EffectivePolicyEntry(PolicyType.DISCORD_GUILD_ALLOW, guild_id, "bootstrap")
        for guild_id in allowed_guilds
    ]
    entries.extend(
        EffectivePolicyEntry(PolicyType.DISCORD_GUILD_BLOCK, guild_id, "bootstrap")
        for guild_id in blocked_guilds
    )
    return BridgePolicySnapshot(tuple(entries))


def _input(
    *,
    guild_id: int | None,
    snapshot: BridgePolicySnapshot | None = None,
    database: object | None = None,
    manage_guild: bool = False,
    ban_decision: BanDecision | None = None,
) -> CommandAccessInput:
    """Build one policy input with explicit policy and moderation boundaries."""
    return CommandAccessInput(
        settings=SimpleNamespace(),
        database=database,
        discord_guild_id=guild_id,
        discord_user_id="123",
        member_can_manage_guild=manage_guild,
        policy_service=_PolicyService(snapshot or _snapshot()),
        ban_service=_BanService(ban_decision),
    )


def test_globally_banned_user_is_rejected_before_guild_or_registration_work() -> None:
    database = MagicMock()
    result = evaluate_policy(
        REGISTERED_GUILD_COMMAND_ACCESS,
        _input(
            guild_id=2,
            database=database,
            ban_decision=BanDecision(True, scope="global", reason="abuse"),
        ),
    )

    assert result.reason == "globally_banned_user"
    assert result.message == "You were banned from this bridge instance.\nReason: abuse"
    database.users.get_user_by_discord_user_id.assert_not_called()


def test_missing_guild_short_circuits_before_registration_lookup() -> None:
    database = MagicMock()
    result = evaluate_policy(
        REGISTERED_GUILD_COMMAND_ACCESS,
        _input(guild_id=None, database=database),
    )

    assert result.reason == "no_guild"
    database.users.get_user_by_discord_user_id.assert_not_called()


def test_blocklisted_guild_short_circuits_before_registration_lookup() -> None:
    database = MagicMock()
    result = evaluate_policy(
        REGISTERED_GUILD_COMMAND_ACCESS,
        _input(guild_id=2, snapshot=_snapshot(blocked_guilds=("2",)), database=database),
    )

    assert result.reason == "guild_not_allowed"
    database.users.get_user_by_discord_user_id.assert_not_called()


def test_empty_effective_allowlist_allows_any_non_blocklisted_guild() -> None:
    result = evaluate_policy(GUILD_COMMAND_ACCESS, _input(guild_id=2))

    assert result.allowed is True


def test_dynamic_or_bootstrap_allowlist_permits_listed_guild_only() -> None:
    snapshot = _snapshot(allowed_guilds=("2",))

    assert evaluate_policy(GUILD_COMMAND_ACCESS, _input(guild_id=2, snapshot=snapshot)).allowed
    denied = evaluate_policy(GUILD_COMMAND_ACCESS, _input(guild_id=3, snapshot=snapshot))
    assert denied.reason == "guild_not_allowed"


def test_blocklist_overrides_allowlist_for_same_guild() -> None:
    snapshot = _snapshot(allowed_guilds=("2",), blocked_guilds=("2",))

    result = evaluate_policy(GUILD_COMMAND_ACCESS, _input(guild_id=2, snapshot=snapshot))

    assert result.reason == "guild_not_allowed"


def test_registered_policy_rejects_unknown_user_with_existing_message() -> None:
    database = MagicMock()
    database.users.get_user_by_discord_user_id.return_value = None
    result = evaluate_policy(
        REGISTERED_GUILD_COMMAND_ACCESS,
        _input(guild_id=2, database=database),
    )

    assert result.reason == "discord_user_not_registered"
    assert result.message == REGISTRATION_REQUIRED_MESSAGE


def test_registered_policy_permits_known_user_and_reads_registration_once() -> None:
    database = MagicMock()
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    value = _input(guild_id=2, database=database)

    result = evaluate_policy(REGISTERED_GUILD_COMMAND_ACCESS, value)

    assert result.allowed is True
    database.users.get_user_by_discord_user_id.assert_called_once_with("123")


def test_manage_guild_policy_requires_native_permission_after_bridge_access() -> None:
    denied = evaluate_policy(
        MANAGE_GUILD_COMMAND_ACCESS,
        _input(guild_id=2, manage_guild=False),
    )
    allowed = evaluate_policy(
        MANAGE_GUILD_COMMAND_ACCESS,
        _input(guild_id=2, manage_guild=True),
    )

    assert denied.reason == "missing_manage_guild"
    assert allowed.allowed is True
