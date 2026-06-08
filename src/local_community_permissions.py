"""Authorization policy for local-community management commands.

Super-admin decisions are evaluated from one immutable effective policy
snapshot so bootstrap and dynamic administrators behave identically.
"""

from __future__ import annotations

from .bridge_policy import BridgePolicyService, BridgePolicySnapshot
from .models import LocalCommunity


def _resolve_snapshot(*, policy_snapshot: BridgePolicySnapshot | None, settings: object | None) -> BridgePolicySnapshot:
    """Resolve effective authorization state for new and compatibility callers."""
    if policy_snapshot is not None:
        return policy_snapshot
    if settings is None:
        return BridgePolicySnapshot(())
    # Compatibility callers only have bootstrap settings and no dynamic DB.
    repository = type("EmptyPolicyRepository", (), {"list_all_active": lambda self: []})()
    return BridgePolicyService(settings=settings, repository=repository).snapshot()


def is_super_admin(*, discord_user_id: str, policy_snapshot: BridgePolicySnapshot | None = None, settings: object | None = None) -> bool:
    """Return whether the Discord user is an effective bridge super-admin."""
    return _resolve_snapshot(policy_snapshot=policy_snapshot, settings=settings).is_super_admin(discord_user_id)


def can_manage_local_community(*, discord_user_id: str, local_community: LocalCommunity, policy_snapshot: BridgePolicySnapshot | None = None, settings: object | None = None) -> bool:
    """Allow effective super-admins or the persisted community owner."""
    if is_super_admin(policy_snapshot=policy_snapshot, settings=settings, discord_user_id=discord_user_id):
        return True
    owner_id = getattr(local_community, "created_by_discord_user_id", None)
    return owner_id is not None and owner_id == discord_user_id


def is_same_guild(*, discord_guild_id: int | None, local_community: LocalCommunity) -> bool:
    """Return whether the command guild matches the community's owning guild."""
    return discord_guild_id == getattr(local_community, "discord_guild_id", None)


def can_access_local_community_from_guild(*, discord_user_id: str, discord_guild_id: int | None, local_community: LocalCommunity, include_disabled: bool = False, policy_snapshot: BridgePolicySnapshot | None = None, settings: object | None = None) -> bool:
    """Allow same-guild owners/users and cross-guild effective super-admins."""
    status = getattr(local_community, "status", None)
    if include_disabled:
        if status not in {"active", "disabled"}:
            return False
    elif status != "active":
        return False
    if is_super_admin(policy_snapshot=policy_snapshot, settings=settings, discord_user_id=discord_user_id):
        return True
    return is_same_guild(discord_guild_id=discord_guild_id, local_community=local_community)
