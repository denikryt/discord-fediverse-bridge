"""Authoritative Discord surface predicates shared by commands and routing."""

from __future__ import annotations

import discord


def is_normal_guild_text_channel(channel: object) -> bool:
    """Admit only protocol ``GUILD_TEXT`` channels, never announcement channels."""
    return (
        isinstance(channel, discord.TextChannel)
        and channel.type is discord.ChannelType.text
    )


def is_personal_feed_root_channel(channel: object) -> bool:
    """Admit concrete guild text-like channels supported by personal-feed roots.

    Discord API announcement channels are ``GUILD_ANNOUNCEMENT`` (type 5).
    Discord.py exposes that protocol type as ``ChannelType.news`` and still
    represents those channels as ``TextChannel``. The protocol ``ChannelType``
    check preserves the product boundary while rejecting forged duck-typed
    objects.
    """
    return isinstance(channel, discord.TextChannel) and channel.type in {
        discord.ChannelType.text,
        discord.ChannelType.news,
    }
