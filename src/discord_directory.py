"""Discord snapshot capture helpers used by command and startup edges.

The HTTP dashboard reads guild/channel names only from the database. These
helpers sit at Discord-facing edges where concrete guild and forum-channel
objects are already available, keeping public dashboard requests independent of
Discord cache or network state.
"""

from __future__ import annotations

from typing import Any

from .db import Database


def record_discord_placement_snapshot(
    database: Database,
    *,
    guild: Any | None,
    channel: Any | None,
) -> None:
    """Persist last-known guild and channel labels from Discord objects."""
    if guild is not None and getattr(guild, "id", None) is not None:
        database.discord_directory.upsert_guild_snapshot(
            discord_guild_id=int(guild.id),
            guild_name=str(getattr(guild, "name", "Unknown guild") or "Unknown guild"),
        )
    if channel is None or getattr(channel, "id", None) is None:
        return

    # Prefer the guild id on the channel object when Discord exposes it. The
    # interaction guild remains a safe fallback for command adapters.
    channel_guild_id = getattr(channel, "guild", None)
    discord_guild_id = getattr(channel_guild_id, "id", None)
    if discord_guild_id is None and guild is not None:
        discord_guild_id = getattr(guild, "id", None)
    database.discord_directory.upsert_channel_snapshot(
        discord_channel_id=int(channel.id),
        discord_guild_id=int(discord_guild_id) if discord_guild_id is not None else None,
        channel_name=str(getattr(channel, "name", "Unknown forum channel") or "Unknown forum channel"),
        channel_type=_channel_type_label(channel),
    )


def refresh_discord_directory_from_bot(database: Database, bot: Any) -> None:
    """Refresh visible guild/forum snapshots from the connected Discord bot."""
    for guild in getattr(bot, "guilds", []) or []:
        record_discord_placement_snapshot(database, guild=guild, channel=None)
        # Discord.py guilds expose forum channels through guild.forums. Tests use
        # the same small attribute contract so this stays a bounded cache pass.
        for channel in getattr(guild, "forums", []) or []:
            record_discord_placement_snapshot(database, guild=guild, channel=channel)


def _channel_type_label(channel: Any) -> str:
    """Return a compact channel type label safe for snapshot storage."""
    channel_type = getattr(channel, "type", None)
    if channel_type is not None:
        return str(getattr(channel_type, "name", channel_type))
    return channel.__class__.__name__
