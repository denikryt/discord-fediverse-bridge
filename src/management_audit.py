"""Constants and small helpers for management audit event persistence.

This module defines the v1 management-audit vocabulary and canonical JSON
serialization rules. It intentionally has no Discord SDK or database-session
knowledge so operations and repositories can share the same domain contract.
"""

from __future__ import annotations

import json
from typing import Any

ACTION_COMMUNITY_CREATED = "community.created"
ACTION_COMMUNITY_METADATA_UPDATED = "community.metadata_updated"
ACTION_COMMUNITY_STATUS_CHANGED = "community.status_changed"
ACTION_BAN_CREATED = "ban.created"
ACTION_BAN_REACTIVATED = "ban.reactivated"
ACTION_BAN_REMOVED = "ban.removed"
ACTION_COMMUNITY_CREATE_FORBIDDEN = "community.create_forbidden"
ACTION_COMMUNITY_MANAGE_FORBIDDEN = "community.manage_forbidden"
ACTION_BAN_CREATE_FORBIDDEN = "ban.create_forbidden"
ACTION_BAN_REMOVE_FORBIDDEN = "ban.remove_forbidden"

ACTION_GUILD_INVITE_PUBLISHED = "guild_invite.published"
ACTION_GUILD_INVITE_REPLACED = "guild_invite.replaced"
ACTION_GUILD_INVITE_PUBLISH_FORBIDDEN = "guild_invite.publish_forbidden"
ACTION_GUILD_INVITE_REMOVED = "guild_invite.removed"
ACTION_GUILD_INVITE_REMOVE_FORBIDDEN = "guild_invite.remove_forbidden"
ACTION_BRIDGE_POLICY_ADDED = "bridge_policy.added"
ACTION_BRIDGE_POLICY_REACTIVATED = "bridge_policy.reactivated"
ACTION_BRIDGE_POLICY_REMOVED = "bridge_policy.removed"
ACTION_BRIDGE_POLICY_MANAGE_FORBIDDEN = "bridge_policy.manage_forbidden"

RESULT_SUCCESS = "success"
RESULT_FORBIDDEN = "forbidden"

REASON_NOT_SUPER_ADMIN = "not_super_admin"
REASON_NOT_OWNER_OR_SUPER_ADMIN = "not_owner_or_super_admin"
REASON_COMMUNITY_DISABLED = "community_disabled"
REASON_MISSING_MANAGE_GUILD = "missing_manage_guild"
REASON_NOT_EFFECTIVE_SUPER_ADMIN = "not_effective_super_admin"

TARGET_LOCAL_COMMUNITY = "local_community"
TARGET_REMOTE_ACTOR = "remote_actor"
TARGET_DISCORD_GUILD = "discord_guild"
TARGET_BRIDGE_POLICY_ENTRY = "bridge_policy_entry"

VALID_ACTIONS = frozenset(
    {
        ACTION_COMMUNITY_CREATED,
        ACTION_COMMUNITY_METADATA_UPDATED,
        ACTION_COMMUNITY_STATUS_CHANGED,
        ACTION_BAN_CREATED,
        ACTION_BAN_REACTIVATED,
        ACTION_BAN_REMOVED,
        ACTION_COMMUNITY_CREATE_FORBIDDEN,
        ACTION_COMMUNITY_MANAGE_FORBIDDEN,
        ACTION_BAN_CREATE_FORBIDDEN,
        ACTION_BAN_REMOVE_FORBIDDEN,
        ACTION_GUILD_INVITE_PUBLISHED,
        ACTION_GUILD_INVITE_REPLACED,
        ACTION_GUILD_INVITE_PUBLISH_FORBIDDEN,
        ACTION_GUILD_INVITE_REMOVED,
        ACTION_GUILD_INVITE_REMOVE_FORBIDDEN,
        ACTION_BRIDGE_POLICY_ADDED,
        ACTION_BRIDGE_POLICY_REACTIVATED,
        ACTION_BRIDGE_POLICY_REMOVED,
        ACTION_BRIDGE_POLICY_MANAGE_FORBIDDEN,
    }
)
VALID_RESULTS = frozenset({RESULT_SUCCESS, RESULT_FORBIDDEN})
VALID_REASON_CODES = frozenset(
    {REASON_NOT_SUPER_ADMIN, REASON_NOT_OWNER_OR_SUPER_ADMIN, REASON_COMMUNITY_DISABLED, REASON_MISSING_MANAGE_GUILD, REASON_NOT_EFFECTIVE_SUPER_ADMIN}
)
VALID_TARGET_TYPES = frozenset({TARGET_LOCAL_COMMUNITY, TARGET_REMOTE_ACTOR, TARGET_DISCORD_GUILD, TARGET_BRIDGE_POLICY_ENTRY})


def canonical_json(payload: dict[str, object] | None) -> str | None:
    """Serialize an audit payload as stable compact JSON text.

    The database stores audit before/after data as TEXT to keep schema filtering
    explicit. Stable key ordering makes tests deterministic and keeps future
    ad-hoc comparisons straightforward.
    """
    if payload is None:
        return None
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def changed_fields(
    before: dict[str, Any],
    after: dict[str, Any],
    fields: tuple[str, ...],
) -> tuple[dict[str, object], dict[str, object]]:
    """Return before/after dicts containing only fields whose values changed."""
    before_delta: dict[str, object] = {}
    after_delta: dict[str, object] = {}
    for field in fields:
        if before.get(field) != after.get(field):
            before_delta[field] = before.get(field)
            after_delta[field] = after.get(field)
    return before_delta, after_delta


def community_created_after(*, community: object) -> dict[str, object]:
    """Build the safe creation snapshot for a local-community audit event."""
    return {
        "created_by_discord_user_id": getattr(community, "created_by_discord_user_id"),
        "discord_forum_channel_id": getattr(community, "discord_forum_channel_id"),
        "discord_guild_id": getattr(community, "discord_guild_id"),
        "display_name": getattr(community, "display_name"),
        "slug": getattr(community, "slug"),
        "status": getattr(community, "status"),
        "summary": getattr(community, "summary"),
    }


def community_metadata_diff(
    *,
    old_display_name: str,
    old_summary: str | None,
    new_display_name: str,
    new_summary: str | None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return changed display metadata fields for a community edit."""
    return changed_fields(
        {"display_name": old_display_name, "summary": old_summary},
        {"display_name": new_display_name, "summary": new_summary},
        ("display_name", "summary"),
    )


def community_status_diff(
    *,
    old_status: str,
    new_status: str,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return the lifecycle-status delta for a community edit."""
    return changed_fields({"status": old_status}, {"status": new_status}, ("status",))
