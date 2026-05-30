"""Discord command adapter tests for `/ban-user`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.commands import ban_user


@pytest.mark.asyncio
async def test_ban_user_command_returns_ephemeral_operation_message(command_tree, interaction, database) -> None:
    """The adapter keeps moderation command output private in Discord."""
    settings = SimpleNamespace(local_community_operator_allowlist=[])
    database.local_communities.get_local_community_by_slug.return_value = SimpleNamespace(
        id=1,
        slug="cats",
        created_by_discord_user_id="1234567890",
    )
    database.community_actor_bans.get_active_ban_by_handle.return_value = None

    ban_user.register(command_tree, database, settings)
    command = command_tree.commands["ban-user"]
    await command.callback(interaction, "cats", "alice@example.com", "spam")

    interaction.response.send_message.assert_awaited_once_with(
        "Banned alice@example.com from community cats.\nReason: spam",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_ban_user_command_passes_interaction_user_id_to_runtime_policy(command_tree, interaction, database) -> None:
    """Runtime ownership uses the Discord caller id supplied by the adapter."""
    settings = SimpleNamespace(local_community_operator_allowlist=[])
    database.local_communities.get_local_community_by_slug.return_value = SimpleNamespace(
        id=1,
        slug="cats",
        created_by_discord_user_id="someone-else",
    )

    ban_user.register(command_tree, database, settings)
    command = command_tree.commands["ban-user"]
    await command.callback(interaction, "cats", "alice@example.com", "spam")

    interaction.response.send_message.assert_awaited_once_with(
        "You are not allowed to manage this local community.",
        ephemeral=True,
    )
    database.community_actor_bans.create_active_ban.assert_not_called()
