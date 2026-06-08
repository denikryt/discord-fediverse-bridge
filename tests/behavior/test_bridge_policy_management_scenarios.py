"""Behavior scenarios for private bridge-policy management commands."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands

from src.bridge_policy import BridgePolicyService
from src.commands.bridge_policy import register
from src.config import Settings
from src.db import Database
from src.user_bans import UserBanService


def _settings(**overrides: str) -> Settings:
    """Build one valid bootstrap configuration for command scenarios."""
    values = {
        "DISCORD_TOKEN": "token",
        "PUBLIC_BASE_URL": "https://bridge.example",
        "FEDIFY_SHARED_SECRET": "secret",
        "BRIDGE_SUPER_ADMIN_USER_IDS": "100",
    }
    values.update(overrides)
    return Settings.model_validate(values)


def _database(tmp_path: Path) -> Database:
    """Create one migrated SQLite database for observable persistence checks."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.migrate()
    return database


def _command_tree(
    *,
    database: Database,
    settings: Settings,
) -> tuple[app_commands.CommandTree, BridgePolicyService]:
    """Register the real command group without connecting a Discord client."""
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    policy_service = BridgePolicyService(
        settings=settings,
        repository=database.bridge_policy_entries,
    )
    register(
        tree,
        database,
        policy_service,
        UserBanService(database=database, settings=settings),
    )
    return tree, policy_service


def _callback(tree: app_commands.CommandTree, name: str):
    """Return one registered public subcommand callback by name."""
    group = tree.get_command("bridge-policy")
    assert isinstance(group, app_commands.Group)
    command = group.get_command(name)
    assert isinstance(command, app_commands.Command)
    return command.callback


def _interaction(*, user_id: int, guild_id: int | None) -> SimpleNamespace:
    """Build the Discord response boundary observed by command scenarios."""
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=guild_id,
        response=SimpleNamespace(send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _choice(value: str) -> app_commands.Choice[str]:
    """Build one Discord policy-type choice passed to the real callback."""
    return app_commands.Choice(name=value, value=value)


@pytest.mark.asyncio
async def test_super_admin_adds_dynamic_block_and_command_commits_audit(
    tmp_path: Path,
) -> None:
    """A super-admin add action persists one active row and one audit event."""
    database = _database(tmp_path)
    tree, _ = _command_tree(database=database, settings=_settings())
    interaction = _interaction(user_id=100, guild_id=200)

    await _callback(tree, "add")(
        interaction,
        _choice("federation-block"),
        "Remote.Example.",
        "abuse",
    )

    entry = database.bridge_policy_entries.get_by_type_and_subject(
        policy_type="federation_block",
        normalized_subject="remote.example",
    )
    events = database.management_audit_events.list_oldest_first()
    assert entry is not None
    assert entry.status == "active"
    assert entry.reason == "abuse"
    assert [event.action for event in events] == ["bridge_policy.added"]
    interaction.response.send_message.assert_awaited_once_with(
        "Added `remote.example` in `federation-block`.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_non_super_admin_cannot_mutate_or_read_policy(
    tmp_path: Path,
) -> None:
    """An unauthorized caller gets no policy data and creates no policy row."""
    database = _database(tmp_path)
    tree, _ = _command_tree(database=database, settings=_settings())
    add_interaction = _interaction(user_id=999, guild_id=200)
    list_interaction = _interaction(user_id=999, guild_id=200)

    await _callback(tree, "add")(
        add_interaction,
        _choice("guild-block"),
        "200",
        None,
    )
    await _callback(tree, "list")(
        list_interaction,
        _choice("super-admin"),
    )

    assert database.bridge_policy_entries.list_all_active() == []
    events = database.management_audit_events.list_oldest_first()
    assert [event.action for event in events] == ["bridge_policy.manage_forbidden"]
    add_interaction.response.send_message.assert_awaited_once_with(
        "Only a bridge super-admin can manage bridge policy.",
        ephemeral=True,
    )
    list_interaction.response.send_message.assert_awaited_once_with(
        "Only a bridge super-admin can list bridge policy.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_bootstrap_policy_entry_cannot_be_removed_through_discord(
    tmp_path: Path,
) -> None:
    """Discord management cannot deactivate an immutable bootstrap entry."""
    database = _database(tmp_path)
    tree, _ = _command_tree(
        database=database,
        settings=_settings(FEDERATION_BLOCKLIST="blocked.example"),
    )
    interaction = _interaction(user_id=100, guild_id=200)

    await _callback(tree, "remove")(
        interaction,
        _choice("federation-block"),
        "blocked.example",
        None,
    )

    assert database.bridge_policy_entries.list_all_active() == []
    assert database.management_audit_events.list_oldest_first() == []
    interaction.response.send_message.assert_awaited_once_with(
        "Bootstrap policy entries cannot be changed through Discord.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_blocking_current_guild_denies_later_policy_commands_there(
    tmp_path: Path,
) -> None:
    """A newly blocked guild cannot run later management commands there."""
    database = _database(tmp_path)
    tree, _ = _command_tree(database=database, settings=_settings())
    add_interaction = _interaction(user_id=100, guild_id=200)
    list_interaction = _interaction(user_id=100, guild_id=200)

    await _callback(tree, "add")(
        add_interaction,
        _choice("guild-block"),
        "200",
        "maintenance",
    )
    await _callback(tree, "list")(
        list_interaction,
        _choice("guild-block"),
    )

    entry = database.bridge_policy_entries.get_by_type_and_subject(
        policy_type="discord_guild_block",
        normalized_subject="200",
    )
    assert entry is not None and entry.status == "active"
    list_interaction.response.send_message.assert_awaited_once_with(
        "The bridge is not allowed to operate in this Discord server.",
        ephemeral=True,
    )
    assert [event.action for event in database.management_audit_events.list_oldest_first()] == [
        "bridge_policy.added"
    ]


@pytest.mark.asyncio
async def test_super_admin_repairs_blocked_guild_from_dm(
    tmp_path: Path,
) -> None:
    """DM removal re-enables a guild without a guild-policy bypass in that guild."""
    database = _database(tmp_path)
    tree, policy_service = _command_tree(database=database, settings=_settings())
    guild_interaction = _interaction(user_id=100, guild_id=200)
    dm_interaction = _interaction(user_id=100, guild_id=None)

    await _callback(tree, "add")(
        guild_interaction,
        _choice("guild-block"),
        "200",
        None,
    )
    await _callback(tree, "remove")(
        dm_interaction,
        _choice("guild-block"),
        "200",
        "reopen",
    )

    entry = database.bridge_policy_entries.get_by_type_and_subject(
        policy_type="discord_guild_block",
        normalized_subject="200",
    )
    assert entry is not None and entry.status == "inactive"
    assert policy_service.snapshot().is_discord_guild_allowed(200) is True
    assert [event.action for event in database.management_audit_events.list_oldest_first()] == [
        "bridge_policy.added",
        "bridge_policy.removed",
    ]
    dm_interaction.response.send_message.assert_awaited_once_with(
        "Removed `200` from `guild-block`.",
        ephemeral=False,
    )


@pytest.mark.asyncio
async def test_removed_entry_reactivates_without_duplicate_row(
    tmp_path: Path,
) -> None:
    """Add after remove reuses the inactive row and records reactivation."""
    database = _database(tmp_path)
    tree, _ = _command_tree(database=database, settings=_settings())

    await _callback(tree, "add")(
        _interaction(user_id=100, guild_id=200),
        _choice("federation-allow"),
        "remote.example",
        "initial",
    )
    await _callback(tree, "remove")(
        _interaction(user_id=100, guild_id=200),
        _choice("federation-allow"),
        "remote.example",
        "pause",
    )
    await _callback(tree, "add")(
        _interaction(user_id=100, guild_id=200),
        _choice("federation-allow"),
        "REMOTE.EXAMPLE.",
        "restored",
    )

    entry = database.bridge_policy_entries.get_by_type_and_subject(
        policy_type="federation_allow",
        normalized_subject="remote.example",
    )
    assert entry is not None
    assert entry.status == "active"
    assert entry.reason == "restored"
    assert [event.action for event in database.management_audit_events.list_oldest_first()] == [
        "bridge_policy.added",
        "bridge_policy.removed",
        "bridge_policy.reactivated",
    ]
