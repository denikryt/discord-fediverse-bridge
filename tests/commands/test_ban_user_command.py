"""Discord command adapter tests for `/ban-user`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.commands import ban_user


@pytest.mark.asyncio
async def test_ban_user_command_returns_ephemeral_operation_message(command_tree, interaction, database) -> None:
    """The adapter keeps moderation command output private in Discord."""
    settings = SimpleNamespace(local_community_operator_allowlist=["1234567890"])
    database.local_communities.get_local_community_by_slug.return_value = SimpleNamespace(id=1, slug="cats")
    database.community_actor_bans.get_active_ban_by_handle.return_value = None

    ban_user.register(command_tree, database, settings)
    command = command_tree.commands["ban-user"]
    await command.callback(interaction, "cats", "alice@example.com", "spam")

    interaction.response.send_message.assert_awaited_once_with(
        "Banned alice@example.com from community cats.\nReason: spam",
        ephemeral=True,
    )
