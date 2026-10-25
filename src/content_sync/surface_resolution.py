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
    CommunityMessageGroup, CommunityThreadGroup, LocalCommunityMessage, LocalCommunityThread,
    MessageMapping, PublishedActivityObject, User,
)
from ..platform_types import ChannelRef, MessageRef


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


@dataclass(frozen=True, slots=True)
class VerifiedCommunityMutationSource:
    """Immutable identity evidence, rechecked when canonical mutation commits."""

    object_id: str
    actor_url: str
    title: str | None
    guild_id: int


def resolve_community_mutation_source(
    session: Session, source: MessageRef, *, external_guild_id: str | None,
    actor_external_id: str | None,
) -> VerifiedCommunityMutationSource | None:
    """Require one current authored Community object for the exact physical ref.

    Never reinterpret an inbound/mirror message as a human-authored source, and
    never use old numeric projections to choose between identical message IDs.
    An absent raw Delete actor is permitted only because the persisted source
    tuple and actor ownership remain independently verifiable here.
    """
    connector = session.scalar(select(PlatformConnector).where(
        PlatformConnector.connector_key == source.connector_key,
        PlatformConnector.platform == "discord",
    ))
    if connector is None:
        return None
    published = session.scalar(select(PublishedActivityObject).where(
        PublishedActivityObject.source_connector_id == connector.id,
        PublishedActivityObject.source_external_container_id == source.external_container_id,
        PublishedActivityObject.source_external_message_id == source.external_message_id,
        PublishedActivityObject.publication_context == "community",
    ))
    if published is None or published.kind not in ("post", "comment"):
        return None
    mapping = session.scalar(select(MessageMapping).where(
        MessageMapping.source_connector_id == connector.id,
        MessageMapping.source_external_container_id == source.external_container_id,
        MessageMapping.source_id == source.external_message_id,
    ))
    if (mapping is None or mapping.object_id != published.object_id
        or mapping.activity_id != published.activity_id
        or mapping.source_channel_surface_id != published.source_channel_surface_id
        or mapping.actor_url != published.actor_url
        or mapping.community_actor_url != published.community_actor_url):
        return None
    user = session.scalar(select(User).where(User.actor_url == published.actor_url))
    if user is None or (actor_external_id is not None and user.discord_user_id != actor_external_id):
        return None
    if published.source_channel_surface_id is None:
        return None
    surface = session.get(ChannelSurface, published.source_channel_surface_id)
    if surface is None or surface.connector_id != connector.id or surface.kind != "forum":
        return None
    # A legacy numeric forum projection may coexist for Discord, but it must
    # never contradict the owner of the exact typed source tuple.
    if (mapping.discord_channel_id is not None and (
        not surface.external_channel_id.isdecimal()
        or mapping.discord_channel_id != int(surface.external_channel_id)
    )):
        return None
    if (surface.external_guild_id is None
        or not surface.external_guild_id.isdecimal()
        or (external_guild_id is not None and external_guild_id != surface.external_guild_id)):
        return None
    current = resolve_forum_source(session, ChannelRef(
        source.connector_key, surface.external_channel_id,
        kind="forum", external_guild_id=surface.external_guild_id,
    ))
    if (current is None or current.channel_surface_id != surface.id
        or current.community_key != published.community_actor_url):
        return None
    is_post = published.kind == "post"
    # Source rows and the published AP object must identify exactly the same
    # immutable event; the surface role by itself is never author provenance.
    if current.owner_domain == "local":
        if is_post:
            row = session.scalar(select(LocalCommunityThread).where(
                LocalCommunityThread.ap_object_id == published.object_id,
                LocalCommunityThread.source_connector_id == connector.id,
                LocalCommunityThread.source_external_thread_id == source.external_container_id,
                LocalCommunityThread.source_external_starter_message_id == source.external_message_id,
            ))
            row_community_id = row.local_community_id if row else None
        else:
            row = session.scalar(select(LocalCommunityMessage).where(
                LocalCommunityMessage.ap_object_id == published.object_id,
                LocalCommunityMessage.source_connector_id == connector.id,
                LocalCommunityMessage.source_external_container_id == source.external_container_id,
                LocalCommunityMessage.source_external_message_id == source.external_message_id,
            ))
            parent = session.get(LocalCommunityThread, row.local_community_thread_id) if row else None
            row_community_id = parent.local_community_id if parent else None
        if current.owner_role == "local_host":
            expected_community_id = current.owner_id
        else:
            subscriber = session.get(LocalSubscriber, current.owner_id)
            expected_community_id = subscriber.local_community_id if subscriber else None
        if (row is None or row_community_id != expected_community_id
            or row.source_channel_surface_id != surface.id
            or row.direction != 'discord_to_ap'):
            return None
    elif current.owner_domain == "remote":
        if is_post:
            row = session.scalar(select(CommunityThreadGroup).where(
                CommunityThreadGroup.ap_object_id == published.object_id,
                CommunityThreadGroup.community_actor_id == current.community_key,
                CommunityThreadGroup.source_connector_id == connector.id,
                CommunityThreadGroup.source_external_thread_id == source.external_container_id,
            ))
            # Remote canonical post starter remains separate from its thread ID.
            if (row is None or not source.external_message_id.isdecimal()
                or row.source_starter_message_id != int(source.external_message_id)):
                return None
        else:
            row = session.scalar(select(CommunityMessageGroup).where(
                CommunityMessageGroup.ap_object_id == published.object_id,
                CommunityMessageGroup.community_actor_id == current.community_key,
                CommunityMessageGroup.source_connector_id == connector.id,
                CommunityMessageGroup.source_external_container_id == source.external_container_id,
                CommunityMessageGroup.source_external_message_id == source.external_message_id,
            ))
        if row is None or row.source_channel_surface_id != surface.id:
            return None
    else:
        return None
    return VerifiedCommunityMutationSource(
        published.object_id, published.actor_url, published.title,
        int(surface.external_guild_id),
    )
