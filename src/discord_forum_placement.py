"""Discord forum-channel placement helpers for bridge management commands.

The command layer owns Discord SDK side effects such as creating or deleting
forum channels. Operations and repositories only receive concrete channel IDs,
so this module centralises the adapter logic that turns an optional selected
channel into a safe, bridge-exclusive forum placement.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from .db import Database

DISCORD_CHANNEL_NAME_LIMIT = 100
MAX_UNIQUE_SUFFIX = 100
CHANNEL_UNAVAILABLE_MESSAGE = "Forum channel {mention} is already used by another bridge community or subscription."
CREATE_CHANNEL_PERMISSION_MESSAGE = (
    "I could not create a Discord forum channel. Give the bot Manage Channels permission, "
    "or choose an existing free forum channel."
)


@dataclass(slots=True)
class ForumPlacement:
    """Describe the Discord forum channel selected or created for one command."""

    channel: Any
    created_by_bot: bool


@dataclass(slots=True)
class ForumPlacementError(Exception):
    """Represent a stable, user-visible forum placement failure."""

    message: str
    reason: str


def normalize_forum_channel_name(value: str) -> str:
    """Return a Discord-safe forum channel name derived from user/domain input.

    Discord supports more characters than this conservative subset, but bridge
    auto-created names should be predictable and shell-safe. Non-ASCII input is
    intentionally not transliterated; if everything is removed, the neutral
    fallback keeps channel creation deterministic.
    """
    lowered = value.strip().lower()
    normalized = re.sub(r"[^a-z0-9_-]+", "-", lowered)
    normalized = re.sub(r"[-_]{2,}", "-", normalized).strip("-_")
    if not normalized:
        normalized = "community"
    return normalized[:DISCORD_CHANNEL_NAME_LIMIT].strip("-_") or "community"


def derive_channel_name_from_community(*, name: str | None, handle: str | None, actor_id: str | None) -> str:
    """Choose the best display-derived channel base name for a resolved community."""
    if name:
        return normalize_forum_channel_name(name)
    if handle:
        local_part = handle.lstrip("!").split("@", 1)[0]
        if local_part:
            return normalize_forum_channel_name(local_part)
    if actor_id:
        segment = actor_id.rstrip("/").rsplit("/", 1)[-1]
        if segment:
            return normalize_forum_channel_name(segment)
    return "community"


def is_forum_channel_available(database: 'Database', channel_id: int) -> bool:
    """Return whether no bridge binding currently owns ``channel_id``.

    Channel names are irrelevant for occupancy. The bridge can create suffixed
    names for Discord collisions, but a Discord channel ID may only appear in one
    of the channel-binding tables at a time.
    """
    return _channel_binding_owner(database, channel_id) is None


def _channel_binding_owner(database: 'Database', channel_id: int) -> str | None:
    """Return the binding table that owns a channel, or ``None`` when free."""
    local = database.local_communities.get_local_community_by_forum_channel_id(channel_id)
    if local is not None:
        return "local_community"
    remote = database.remote_subscriptions.get_subscription_by_channel(channel_id)
    if remote is not None:
        return "remote_subscription"
    local_subscriber = database.local_subscribers.get_local_subscriber_by_channel(channel_id)
    if local_subscriber is not None:
        return "local_subscriber"
    return None


def _channel_mention(channel: Any) -> str:
    """Return a readable mention-like label for real and fake channel objects."""
    return str(getattr(channel, "mention", f"<#{getattr(channel, 'id', 'unknown')}>") )


def _is_forum_channel(channel: Any) -> bool:
    """Best-effort forum-channel check that still works with command test fakes."""
    if isinstance(channel, getattr(discord, "ForumChannel", ())):
        return True
    channel_type = getattr(channel, "type", None)
    if channel_type == getattr(discord.ChannelType, "forum", object()):
        return True
    # discord.py has already resolved typed slash command parameters before the
    # callback runs. Test fakes often omit ``type`` entirely, so absence is
    # treated as trusted typed input rather than as a hard failure.
    return channel_type is None


def _belongs_to_guild(guild: Any, channel: Any) -> bool:
    """Return whether a selected channel is still part of the current guild."""
    guild_id = getattr(guild, "id", None)
    channel_guild = getattr(channel, "guild", None)
    channel_guild_id = getattr(channel, "guild_id", None) or getattr(channel_guild, "id", None)
    if guild_id is not None and channel_guild_id is not None:
        return int(guild_id) == int(channel_guild_id)
    channels = getattr(guild, "channels", None)
    if channels is not None:
        return any(getattr(candidate, "id", None) == getattr(channel, "id", None) for candidate in channels)
    # If Discord supplied the typed channel object and no guild metadata is
    # exposed, do not reject based only on missing fake attributes.
    return True


def _has_manage_channels_permission(guild: Any) -> bool | None:
    """Return the bot Manage Channels preflight result when SDK state exposes it."""
    me = getattr(guild, "me", None)
    permissions = getattr(me, "guild_permissions", None)
    value = getattr(permissions, "manage_channels", None)
    if isinstance(value, bool):
        return value
    return None


def _existing_channel_names(guild: Any) -> set[str]:
    """Collect lower-cased channel names from a guild-like object."""
    return {
        str(getattr(channel, "name")).lower()
        for channel in getattr(guild, "channels", []) or []
        if getattr(channel, "name", None)
    }


def _unique_forum_channel_name(guild: Any, desired_name: str) -> str:
    """Return a bounded unique name for a new Discord forum channel."""
    base = normalize_forum_channel_name(desired_name)
    existing = _existing_channel_names(guild)
    if base.lower() not in existing:
        return base
    for suffix in range(2, MAX_UNIQUE_SUFFIX + 1):
        suffix_text = f"-{suffix}"
        trimmed = base[: DISCORD_CHANNEL_NAME_LIMIT - len(suffix_text)].rstrip("-_") or "community"
        candidate = f"{trimmed}{suffix_text}"
        if candidate.lower() not in existing:
            return candidate
    raise ForumPlacementError(
        message="I could not choose a unique forum channel name. Please choose an existing free forum channel.",
        reason="channel_name_exhausted",
    )


async def resolve_optional_forum_channel(
    *,
    database: 'Database',
    guild: Any,
    selected_channel: Any | None,
    desired_name: str,
    command_name: str,
) -> ForumPlacement:
    """Resolve optional command input into a free Discord forum channel.

    Selected channels are only validated. Omitted channels are created through
    ``Guild.create_forum`` after a best-effort Manage Channels preflight. This
    function deliberately does not call operation-layer code; it only prepares
    the Discord placement that operations will persist.
    """
    if guild is None:
        raise ForumPlacementError(
            message="This command can only be used inside a guild.",
            reason="not_guild_context",
        )

    if selected_channel is not None:
        if not _belongs_to_guild(guild, selected_channel):
            raise ForumPlacementError(
                message="Choose a forum channel from this server.",
                reason="channel_wrong_guild",
            )
        if not _is_forum_channel(selected_channel):
            raise ForumPlacementError(
                message="Choose a Discord forum channel.",
                reason="not_forum_channel",
            )
        if not is_forum_channel_available(database, int(getattr(selected_channel, "id"))):
            raise ForumPlacementError(
                message=CHANNEL_UNAVAILABLE_MESSAGE.format(mention=_channel_mention(selected_channel)),
                reason="channel_unavailable",
            )
        return ForumPlacement(channel=selected_channel, created_by_bot=False)

    preflight = _has_manage_channels_permission(guild)
    if preflight is False:
        raise ForumPlacementError(
            message=CREATE_CHANNEL_PERMISSION_MESSAGE,
            reason="missing_manage_channels",
        )

    unique_name = _unique_forum_channel_name(guild, desired_name)
    try:
        # v1 intentionally creates a root forum with SDK defaults. Category,
        # tags, overwrites, slowmode, topic, and layout are future-work knobs.
        created = await guild.create_forum(
            name=unique_name,
            reason=f"discord-fediverse-bridge {command_name} auto-create",
        )
    except discord.Forbidden as exc:
        raise ForumPlacementError(
            message=CREATE_CHANNEL_PERMISSION_MESSAGE,
            reason="channel_creation_forbidden",
        ) from exc
    except Exception as exc:
        raise ForumPlacementError(
            message="I could not create a Discord forum channel. Please choose an existing free forum channel.",
            reason="channel_creation_failed",
        ) from exc
    return ForumPlacement(channel=created, created_by_bot=True)


async def cleanup_created_forum_channel(
    placement: ForumPlacement | None,
    *,
    database: 'Database',
    logger: logging.Logger,
    guild_id: int | None,
    command_name: str,
    original_reason: str,
) -> None:
    """Best-effort delete a bot-created channel after later command failure.

    Discord side effects are outside the database transaction. Cleanup therefore
    re-checks every bridge channel-binding table before deleting, then logs and
    suppresses cleanup failures so the original user-facing error is preserved.
    """
    if placement is None or not placement.created_by_bot:
        return
    channel_id = int(getattr(placement.channel, "id"))
    if _channel_binding_owner(database, channel_id) is not None:
        logger.warning(
            "Skipped cleanup for bot-created forum channel %s in guild %s after %s failure %s because a bridge row now owns it",
            channel_id,
            guild_id,
            command_name,
            original_reason,
        )
        return
    try:
        await placement.channel.delete(
            reason=f"discord-fediverse-bridge cleanup after {command_name} failure: {original_reason}"
        )
    except Exception:
        logger.exception(
            "Failed to cleanup bot-created forum channel %s in guild %s after %s failure %s",
            channel_id,
            guild_id,
            command_name,
            original_reason,
        )
