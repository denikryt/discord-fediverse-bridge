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


def can_manage_local_community(
    *,
    settings: Settings,
    discord_user_id: str,
    local_community: LocalCommunity,
) -> bool:
    """Return whether a Discord user may manage one local community.

    The current configured local-community operator allowlist acts as the
    bridge super-admin list for management commands. Super-admins can manage any
    community, including legacy rows whose creator id is still NULL after the
    additive ownership migration. Non-admin callers must match the stored
    creator id exactly as a string.
    """
    # Discord ids are opaque snowflake strings for this policy. Avoid integer
    # coercion so ids such as "123" and "0123" never compare as equal.
    if discord_user_id in settings.local_community_operator_allowlist:
        return True

    owner_id = getattr(local_community, "created_by_discord_user_id", None)
    if owner_id is None:
        # Legacy NULL-owned rows are a development compatibility case. They stay
        # manageable only through the super-admin branch above.
        return False

    return owner_id == discord_user_id
