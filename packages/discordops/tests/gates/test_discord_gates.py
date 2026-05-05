"""Test Discord gate checks."""

import pytest
from unittest.mock import AsyncMock, MagicMock
import discord

from discordops.gates import (
    has_actor_authority,
    require_guild_context,
    require_actor_authority,
)


class TestHasActorAuthority:
    """Tests for has_actor_authority check."""

    def test_returns_true_for_admin_users(self):
        """has_actor_authority returns True for admin users."""
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user.guild_permissions.administrator = True
        assert has_actor_authority(interaction) is True

    def test_returns_false_for_non_admin_users(self):
        """has_actor_authority returns False for non-admin users."""
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user.guild_permissions.administrator = False
        assert has_actor_authority(interaction) is False

    def test_returns_false_when_guild_permissions_is_none(self):
        """has_actor_authority returns False when guild_permissions is None."""
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user.guild_permissions = None
        assert has_actor_authority(interaction) is False

    def test_returns_false_when_user_has_no_guild_permissions(self):
        """has_actor_authority returns False when user has no guild_permissions attribute."""
        interaction = MagicMock(spec=discord.Interaction)
        del interaction.user.guild_permissions
        assert has_actor_authority(interaction) is False


class TestRequireGuildContext:
    """Tests for require_guild_context gate."""

    @pytest.mark.asyncio
    async def test_returns_guild_when_in_guild(self):
        """require_guild_context returns guild if interaction is in a guild."""
        interaction = AsyncMock(spec=discord.Interaction)
        mock_guild = MagicMock(spec=discord.Guild)
        interaction.guild = mock_guild

        result = await require_guild_context(interaction)
        assert result == mock_guild
        interaction.response.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_rejection_and_returns_none_for_dm(self):
        """require_guild_context sends rejection and returns None for DMs."""
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.guild = None
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()

        result = await require_guild_context(interaction)
        assert result is None
        interaction.response.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejection_message_mentions_server(self):
        """require_guild_context rejection message mentions server requirement."""
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.guild = None
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()

        await require_guild_context(interaction)
        call_args = interaction.response.send_message.call_args
        message = call_args[0][0].lower()
        assert "server" in message

    @pytest.mark.asyncio
    async def test_rejection_is_ephemeral(self):
        """require_guild_context sends ephemeral rejection."""
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.guild = None
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()

        await require_guild_context(interaction)
        call_args = interaction.response.send_message.call_args
        assert call_args[1]["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_uses_followup_when_response_already_done(self):
        """require_guild_context uses followup.send when response is done."""
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.guild = None
        interaction.response.is_done.return_value = True
        interaction.followup.send = AsyncMock()

        await require_guild_context(interaction)
        interaction.followup.send.assert_called_once()
        interaction.response.send_message.assert_not_called()


class TestRequireActorAuthority:
    """Tests for require_actor_authority gate."""

    @pytest.mark.asyncio
    async def test_returns_true_for_admin_users(self):
        """require_actor_authority returns True for admin users."""
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.user.guild_permissions.administrator = True

        result = await require_actor_authority(interaction)
        assert result is True
        interaction.response.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_rejection_and_returns_false_for_non_admins(self):
        """require_actor_authority sends rejection and returns False for non-admins."""
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.user.guild_permissions.administrator = False
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()

        result = await require_actor_authority(interaction)
        assert result is False
        interaction.response.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejection_message_mentions_permission(self):
        """require_actor_authority rejection message mentions permission requirement."""
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.user.guild_permissions.administrator = False
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()

        await require_actor_authority(interaction)
        call_args = interaction.response.send_message.call_args
        message = call_args[0][0].lower()
        assert "permission" in message

    @pytest.mark.asyncio
    async def test_rejection_is_ephemeral(self):
        """require_actor_authority sends ephemeral rejection."""
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.user.guild_permissions.administrator = False
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()

        await require_actor_authority(interaction)
        call_args = interaction.response.send_message.call_args
        assert call_args[1]["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_uses_followup_when_response_already_done(self):
        """require_actor_authority uses followup.send when response is done."""
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.user.guild_permissions.administrator = False
        interaction.response.is_done.return_value = True
        interaction.followup.send = AsyncMock()

        await require_actor_authority(interaction)
        interaction.followup.send.assert_called_once()
        interaction.response.send_message.assert_not_called()
