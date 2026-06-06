"""Application workflow for publishing and removing guild invite links."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import discord

from .db import Database

logger = logging.getLogger(__name__)
_GUILD_LOCKS: dict[int, asyncio.Lock] = {}


def _guild_lock(guild_id: int) -> asyncio.Lock:
    """Return the in-process lock serializing invite mutations for one guild."""
    return _GUILD_LOCKS.setdefault(guild_id, asyncio.Lock())


@dataclass(slots=True)
class GuildInviteResult:
    """Describe one guild-invite workflow result for command presentation."""

    kind: str
    invite_url: str | None = None


def _active_host_channel_ids(database: Database, guild_id: int) -> set[int]:
    """Return host channel ids for active local communities in one guild."""
    return {int(row.discord_forum_channel_id) for row in database.local_communities.list_active_local_communities_by_guild(discord_guild_id=guild_id)}


async def publish_guild_invite(*, database: Database, client: Any, guild: Any, channel: Any, actor_discord_user_id: str) -> GuildInviteResult:
    """Serialize and publish an invite for one Discord guild."""
    guild_id = int(guild.id)
    async with _guild_lock(guild_id):
        return await _publish_guild_invite(database=database, client=client, guild=guild, channel=channel, actor_discord_user_id=actor_discord_user_id)


async def _publish_guild_invite(*, database: Database, client: Any, guild: Any, channel: Any, actor_discord_user_id: str) -> GuildInviteResult:
    """Create and persist an invite for an active local-community host channel."""
    guild_id = int(guild.id)
    active_hosts = _active_host_channel_ids(database, guild_id)
    if not active_hosts:
        return GuildInviteResult("no_active_local_community")
    if int(getattr(channel, "id", 0)) not in active_hosts:
        return GuildInviteResult("channel_not_active_local_community_host")
    if int(getattr(getattr(channel, "guild", guild), "id", guild_id)) != guild_id:
        return GuildInviteResult("invalid_channel")

    default_role = getattr(guild, "default_role", None)
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return GuildInviteResult("invalid_channel")
    everyone_permissions = permissions_for(default_role)
    if not bool(getattr(everyone_permissions, "view_channel", False)):
        return GuildInviteResult("private_channel")
    bot_member = getattr(guild, "me", None)
    bot_permissions = permissions_for(bot_member)
    if not bool(getattr(bot_permissions, "create_instant_invite", False)):
        return GuildInviteResult("bot_permission_missing")
    create_invite = getattr(channel, "create_invite", None)
    if not callable(create_invite):
        return GuildInviteResult("invalid_channel")

    try:
        invite = await create_invite(max_age=0, max_uses=0, unique=True, reason="Published on bridge dashboard")
    except Exception:
        logger.exception("Failed to create Discord guild invite")
        return GuildInviteResult("create_invite_failed")

    invite_code = str(getattr(invite, "code", ""))
    invite_url = str(getattr(invite, "url", invite))
    previous = database.guild_invite_publications.get_by_guild_id(guild_id)
    try:
        before, current = database.management_actions.replace_guild_invite_publication(
            discord_guild_id=guild_id,
            discord_channel_id=int(channel.id),
            invite_code=invite_code,
            invite_url=invite_url,
            actor_discord_user_id=actor_discord_user_id,
        )
    except Exception:
        logger.exception("Failed to persist Discord guild invite publication")
        try:
            await invite.delete(reason="Bridge publication persistence failed")
        except Exception:
            logger.exception("Failed to compensate newly created Discord invite")
        return GuildInviteResult("persistence_failed")

    if previous is not None:
        try:
            old_invite = await client.fetch_invite(previous.invite_code)
            await old_invite.delete(reason="Replaced bridge dashboard invite")
        except discord.NotFound:
            pass
        except Exception:
            logger.exception("Failed to delete replaced Discord invite %s", previous.invite_code)
    return GuildInviteResult("replaced" if previous is not None else "published", invite_url=invite_url)


async def remove_guild_invite(*, database: Database, client: Any, guild: Any, actor_discord_user_id: str) -> GuildInviteResult:
    """Serialize removal of one guild invite publication."""
    guild_id = int(guild.id)
    async with _guild_lock(guild_id):
        return await _remove_guild_invite(database=database, client=client, guild=guild, actor_discord_user_id=actor_discord_user_id)


async def _remove_guild_invite(*, database: Database, client: Any, guild: Any, actor_discord_user_id: str) -> GuildInviteResult:
    """Delete the current Discord invite and remove its dashboard publication."""
    guild_id = int(guild.id)
    current = database.guild_invite_publications.get_by_guild_id(guild_id)
    if current is None:
        return GuildInviteResult("not_published")
    try:
        invite = await client.fetch_invite(current.invite_code)
        await invite.delete(reason="Removed from bridge dashboard")
    except discord.NotFound:
        pass
    except Exception:
        logger.exception("Failed to delete published Discord invite")
        return GuildInviteResult("delete_invite_failed")
    try:
        database.management_actions.remove_guild_invite_publication(
            discord_guild_id=guild_id,
            actor_discord_user_id=actor_discord_user_id,
        )
    except Exception:
        logger.exception("Failed to remove persisted guild invite publication")
        return GuildInviteResult("persistence_failed")
    return GuildInviteResult("removed")
