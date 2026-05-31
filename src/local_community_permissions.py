"""Authorization policy for local-community management commands.

This module owns command-side management permissions for bridge-hosted local
communities. It deliberately stays separate from ActivityPub moderation checks:
those decide whether inbound federation events should be skipped, while this
policy decides whether a Discord user may manage a community through bot
commands.
"""

from __future__ import annotations

from .config import Settings
from .models import LocalCommunity


def is_super_admin(*, settings: Settings, discord_user_id: str) -> bool:
    """Return whether the Discord user id is configured as a super-admin.

    The project currently stores the super-admin ids in the historical
    `local_community_operator_allowlist` setting. Keep comparison string-based
    because Discord snowflakes are opaque identifiers, not numbers.
    """
    return discord_user_id in settings.local_community_operator_allowlist


def can_manage_local_community(
    *,
    settings: Settings,
    discord_user_id: str,
    local_community: LocalCommunity,
) -> bool:
    """Return whether a Discord user may manage one local community.

    Super-admins can manage any community, including legacy rows whose creator
    id is NULL. Non-admin callers must match the stored creator id exactly.
    """
    if is_super_admin(settings=settings, discord_user_id=discord_user_id):
        return True

    owner_id = getattr(local_community, "created_by_discord_user_id", None)
    if owner_id is None:
        # Legacy NULL-owned rows are a development compatibility case. They stay
        # manageable only through the super-admin branch above.
        return False

    return owner_id == discord_user_id


def is_same_guild(
    *,
    discord_guild_id: int | None,
    local_community: LocalCommunity,
) -> bool:
    """Return whether the command guild matches the community's owning guild."""
    return discord_guild_id == getattr(local_community, "discord_guild_id", None)


def can_access_local_community_from_guild(
    *,
    settings: Settings,
    discord_user_id: str,
    discord_guild_id: int | None,
    local_community: LocalCommunity,
) -> bool:
    """Return whether a command may address a community from this guild.

    Normal users are scoped to the current guild. Super-admins can manually
    enter a globally unique slug from another guild, so cross-guild access is
    allowed only through the explicit super-admin branch.
    """
    if getattr(local_community, "status", None) != "active":
        # Management/list commands operate only on active communities in this
        # stage. Inactive community lifecycle semantics belong to a later plan.
        return False
    if is_super_admin(settings=settings, discord_user_id=discord_user_id):
        return True
    return is_same_guild(discord_guild_id=discord_guild_id, local_community=local_community)
