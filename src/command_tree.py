"""Discord application command tree without hidden admission policy.

All command authorization belongs to DiscordOps preconditions declared by the
command/operation flow. This tree exists only as the Discord SDK container.
"""

from __future__ import annotations

import discord
from discord import app_commands


class BridgeCommandTree(app_commands.CommandTree):
    """Own registered application commands without a second policy layer."""

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        """Allow dispatch so command-specific DiscordOps preconditions decide access."""
        return True
