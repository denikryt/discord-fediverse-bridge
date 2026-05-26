"""Participant-source resolution for Discord-backed local communities.

The local-community runtime accepts Discord events from two local participant
families: the host forum that owns the community metadata and active local
subscriber forums that Stage 4 makes create-capable.  Keeping that decision in
one helper prevents the router and runtime from duplicating role checks or
accidentally treating inactive subscriber rows as valid sources.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..db import Database


@dataclass(slots=True)
class ResolvedLocalCommunitySource:
    """Describe the local-community participant that owns one Discord forum.

    `source_kind` is intentionally constrained to `host_forum` and
    `local_subscriber` because Stage 4 only widens create routing to active
    same-instance subscribers; remote ActivityPub participants never enter via
    the Discord event router.
    """

    local_community: object
    source_kind: str
    local_subscriber: object | None
    discord_forum_channel_id: int


def resolve_local_community_source_for_forum(
    database: Database,
    forum_channel_id: int | None,
) -> ResolvedLocalCommunitySource | None:
    """Resolve host or active local-subscriber ownership for one forum.

    Host ownership wins over subscriber rows so bad legacy data cannot demote
    the canonical host forum into a subscriber surface.  Inactive or deleted
    local subscribers are deliberately ignored; Stage 4 only routes active
    local subscriber forums as create-capable sources.
    """
    if forum_channel_id is None:
        return None

    local_community = database.get_local_community_by_forum_channel_id(forum_channel_id)
    if local_community is not None:
        return ResolvedLocalCommunitySource(
            local_community=local_community,
            source_kind="host_forum",
            local_subscriber=None,
            discord_forum_channel_id=forum_channel_id,
        )

    # A mixed remote-subscription/local-subscriber forum should not silently
    # switch runtimes in Stage 4.  Stage 1 rejects this during normal control
    # flow, and this defensive check keeps corrupted/manual rows on the older
    # remote-subscription path instead of treating them as local sources.
    if database.get_subscription_by_channel(forum_channel_id) is not None:
        return None

    local_subscriber = database.get_local_subscriber_by_channel(forum_channel_id)
    if local_subscriber is None or getattr(local_subscriber, "status", "active") != "active":
        return None
    local_community = database.get_local_community_by_id(getattr(local_subscriber, "local_community_id"))
    if local_community is None:
        return None
    return ResolvedLocalCommunitySource(
        local_community=local_community,
        source_kind="local_subscriber",
        local_subscriber=local_subscriber,
        discord_forum_channel_id=forum_channel_id,
    )
