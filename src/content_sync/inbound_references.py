"""Shared Discord reply-reference construction for inbound mirrored comments."""

from __future__ import annotations

import discord


def build_message_reference(*, discord_thread: object, message_id: int) -> discord.MessageReference:
    """Build one discord.py-compatible reference for a mirrored reply.

    Both bridge modes only need the parent message id plus the thread channel
    id. `fail_if_not_exists=False` preserves best-effort delivery when Discord
    no longer has the parent message cached locally.
    """
    guild_id = getattr(discord_thread, "guild_id", None)
    guild = getattr(discord_thread, "guild", None)
    if guild_id is None and guild is not None:
        guild_id = getattr(guild, "id", None)

    return discord.MessageReference(
        message_id=message_id,
        channel_id=getattr(discord_thread, "id"),
        guild_id=guild_id,
        fail_if_not_exists=False,
    )
