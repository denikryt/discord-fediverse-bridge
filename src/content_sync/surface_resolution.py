"""Resolve exact connector-scoped forum ownership before community publication.

The caller owns the SQLite session and repeats the resolution at persistence
and physical dispatch. A claim is necessary but never sufficient: the owning
community/subscription lifecycle must still authorize new source activity.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ChannelSurface,
    PlatformConnector,
    LocalCommunity,
    LocalSubscriber,
    ChannelCommunitySubscription,
)
from ..platform_types import ChannelRef


@dataclass(frozen=True, slots=True)
class ResolvedForumSource:
    """Immutable observed source-owner evidence, not a cached permission token.

    Revalidate this object against its source ChannelRef in a new writer
    transaction before admitting the canonical post, since surface ownership
    may change after a preliminary read.
    """

    connector_key: str
    channel_surface_id: int
    external_channel_id: str
    external_guild_id: str
    owner_domain: str
    owner_role: str
    owner_id: int
    community_key: str


def resolve_forum_source(session: Session, channel: ChannelRef) -> ResolvedForumSource | None:
    """Resolve one verified forum source or refuse unknown/revoked ownership.

    No Discord ID casts or SDK calls occur in this shared boundary. Missing
    authenticated guild/kind evidence cannot be substituted by a guessed
    current owner, and a historic physical copy is not a surface claim.
    """
    if not isinstance(channel, ChannelRef) or channel.kind != 'forum' or channel.external_guild_id is None:
        return None

    result = session.execute(
        select(ChannelSurface, PlatformConnector)
        .join(PlatformConnector, PlatformConnector.id == ChannelSurface.connector_id)
        .where(
            PlatformConnector.connector_key == channel.connector_key,
            ChannelSurface.external_channel_id == channel.external_channel_id,
            ChannelSurface.kind == 'forum',
            ChannelSurface.external_guild_id == channel.external_guild_id,
        )
    ).one_or_none()
    if result is None:
        return None
    surface, connector = result
    # A non-null, database-generated role identifies exactly one owner field.
    # Check the actual row lifecycle rather than trusting a cached role string.
    if surface.role == 'local_host' and surface.local_community_id is not None:
        owner = session.get(LocalCommunity, surface.local_community_id)
        if owner is not None and owner.status == 'active':
            return ResolvedForumSource(
                connector.connector_key, surface.id, surface.external_channel_id,
                surface.external_guild_id, 'local', 'local_host', owner.id, owner.actor_url,
            )
    elif surface.role == 'local_subscriber' and surface.local_subscriber_id is not None:
        owner = session.get(LocalSubscriber, surface.local_subscriber_id)
        community = session.get(LocalCommunity, owner.local_community_id) if owner else None
        if owner is not None and owner.status == 'active' and community is not None and community.status == 'active':
            return ResolvedForumSource(
                connector.connector_key, surface.id, surface.external_channel_id,
                surface.external_guild_id, 'local', 'local_subscriber', owner.id, community.actor_url,
            )
    elif surface.role == 'remote_subscription' and surface.remote_subscription_id is not None:
        owner = session.get(ChannelCommunitySubscription, surface.remote_subscription_id)
        if owner is not None and owner.status == 'accepted' and owner.lemmy_community_actor_id:
            return ResolvedForumSource(
                connector.connector_key, surface.id, surface.external_channel_id,
                surface.external_guild_id, 'remote', 'remote_subscription', owner.id,
                owner.lemmy_community_actor_id,
            )
    return None
