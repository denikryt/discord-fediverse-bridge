"""Authoritative Discord surface predicates shared by commands and routing."""

from __future__ import annotations

import discord


def is_normal_guild_text_channel(channel: object) -> bool:
    """Admit only protocol ``GUILD_TEXT`` channels, never announcement/news channels."""
    return isinstance(channel, discord.TextChannel) and channel.type is discord.ChannelType.text
