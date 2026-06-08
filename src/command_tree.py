"""Central Discord application-command policy boundary."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from .user_bans import UserBanService, render_ban_message

logger = logging.getLogger(__name__)


class BridgeCommandTree(app_commands.CommandTree):
    """Reject globally banned callers before any slash-command callback runs."""

    def __init__(self, client: discord.Client, *, ban_service: UserBanService) -> None:
        """Initialise the shared tree with one transport-independent ban policy."""
        super().__init__(client)
        self.ban_service = ban_service

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        """Fail closed on lookup errors and privately reject active global bans."""
        try:
            decision = self.ban_service.check_global_discord_user(str(interaction.user.id))
        except Exception:
            logger.exception("Global command-ban lookup failed for Discord user %s", interaction.user.id)
            try:
                await interaction.response.send_message(
                    "The bridge could not verify command access. Please try again later.",
                    ephemeral=True,
                )
            except Exception:
                logger.exception("Failed to send command access verification error")
            return False
        if not decision.banned:
            return True
        try:
            await interaction.response.send_message(render_ban_message(decision), ephemeral=True)
        except Exception:
            # Callback execution remains denied even when Discord rejects the
            # private response, preserving fail-closed moderation semantics.
            logger.exception("Failed to send global-ban command rejection")
        return False
