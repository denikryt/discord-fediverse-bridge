"""Discord-specific gate checks for common validation scenarios."""

from __future__ import annotations

import discord


async def _send_rejection(interaction: discord.Interaction, message: str) -> None:
    """Send an ephemeral rejection response."""
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def has_actor_authority(interaction: discord.Interaction) -> bool:
    """Return True if user is a guild admin.

    This is a pure check that does not send a response.

    Args:
        interaction: Discord interaction to check.

    Returns:
        True if user holds Administrator permission, False otherwise.
    """
    return getattr(
        getattr(interaction.user, "guild_permissions", None),
        "administrator",
        False,
    )


async def require_guild_context(interaction: discord.Interaction) -> discord.Guild | None:
    """Return guild if interaction is in a guild, else send rejection and return None.

    Use this when you need to ensure the command was invoked from a server,
    not from a DM.

    Args:
        interaction: Discord interaction to check.

    Returns:
        Guild object if interaction is in a server, None otherwise (rejection sent).
    """
    if interaction.guild is None:
        await _send_rejection(interaction, "This command can only be used in a server.")
        return None
    return interaction.guild


async def require_actor_authority(interaction: discord.Interaction) -> bool:
    """Return True if user is admin, else send rejection and return False.

    Combines has_actor_authority check with automatic rejection response.

    Args:
        interaction: Discord interaction to check.

    Returns:
        True if user is admin, False otherwise (rejection sent).
    """
    if not has_actor_authority(interaction):
        await _send_rejection(interaction, "You do not have permission to perform this action.")
        return False
    return True
