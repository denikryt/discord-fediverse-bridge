"""Atomic persistence for canonical Discord-originated ActivityPub creates."""

from __future__ import annotations

from ..db.repositories.platform_surfaces import (
    PlatformSurfaceRepository,
    discord_copy_query,
)

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..platform_types import MessageRef

from ..models import (
    MessageMapping,
    ChannelSurface,
    PlatformConnector,
    PublishedActivityObject,
    User,
    UserActorOutboundSource,
    ChannelCommunitySubscription,
    LocalCommunity,
    InboundConversationObject,
    RemoteActor,
)


class PublicationConflictError(RuntimeError):
    """Signal that durable identity conflicts with an attempted publication."""


def _mapping_by_source(session, source_id: str, source_message: MessageRef | None) -> MessageMapping | None:
    """Select a verified immediate source, refusing ambiguous historical queries.

    The legacy callers can still read an unambiguous Discord projection, but
    cannot ever adopt an arbitrary sibling source with the same message ID.
    """
    if source_message is not None:
        if source_message.connector_key != 'discord-primary':
            # Community publication requires authenticated Discord human sources.
            # Other connectors need equivalent registration and author verification.
            raise PublicationConflictError('Community authored source requires Discord primary connector')
        if source_message.external_message_id != source_id:
            raise PublicationConflictError('Source ID differs from verified MessageRef')
        query = (
            select(MessageMapping)
            .join(PlatformConnector, MessageMapping.source_connector_id == PlatformConnector.id)
            .where(
                PlatformConnector.connector_key == source_message.connector_key,
                PlatformConnector.platform == 'discord',
                MessageMapping.source_external_container_id == source_message.external_container_id,
                MessageMapping.source_id == source_message.external_message_id,
            )
        )
    else:
        query = discord_copy_query(MessageMapping, source=True).where(
            MessageMapping.source_platform == 'discord',
            MessageMapping.source_id == source_id,
        )
    matches = list(session.scalars(query.limit(2)))
    if len(matches) > 1:
        raise PublicationConflictError('Ambiguous source: exact MessageRef is required')
    return matches[0] if matches else None


@dataclass(frozen=True, slots=True)
class PersistPublishResult:
    """Describe whether one canonical publication was newly committed or reused."""

    source_id: int
    created: bool


def _matches(
    mapping: MessageMapping,
    *,
    source_id: str,
    actor_url: str,
    publication_context: str,
    community_actor_url: str | None,
    discord_guild_id: int | None,
    discord_channel_id: int | None,
    discord_message_id: int | None,
    activity_id: str,
    object_id: str,
) -> bool:
    """Return whether one row is the exact deterministic duplicate expected."""
    return (
        mapping.source_platform == "discord"
        and mapping.source_id == source_id
        and mapping.actor_url == actor_url
        and mapping.publication_context == publication_context
        and mapping.community_actor_url == community_actor_url
        and mapping.discord_guild_id == discord_guild_id
        and mapping.discord_channel_id == discord_channel_id
        and mapping.discord_message_id == discord_message_id
        and mapping.activity_id == activity_id
        and mapping.object_id == object_id
    )


def _scope_matches(session, mapping, channel_id, source_message: MessageRef | None = None):
    """Reject duplicate publications belonging to another physical connector."""
    if mapping.source_connector_id is None:
        return False
    connector = session.get(PlatformConnector, mapping.source_connector_id)
    if (
        connector is None
        or connector.connector_key != "discord-primary"
        or connector.platform != "discord"
    ):
        return False
    if source_message is not None:
        if (
            source_message.connector_key != connector.connector_key
            or source_message.external_message_id != mapping.source_id
            or source_message.external_container_id != mapping.source_external_container_id
        ):
            return False
    elif mapping.source_external_container_id is not None:
        # A legacy numeric-only lookup may not accidentally adopt a typed
        # forum starter/reply whose immediate container was never supplied.
        return False
    if channel_id is None:
        return mapping.source_channel_surface_id is None
    if mapping.source_channel_surface_id is None:
        return False
    surface = session.get(ChannelSurface, mapping.source_channel_surface_id)
    return (
        surface is not None
        and surface.connector_id == connector.id
        and surface.external_channel_id == str(channel_id)
    )


def _existing_result(
    session: object,
    mapping: MessageMapping,
    *,
    actor_url: str,
    publication_context: str,
    community_actor_url: str | None,
    discord_guild_id: int | None,
    discord_channel_id: int | None,
    discord_message_id: int | None,
    activity_id: str,
    object_id: str,
    published_at: datetime | None = None,
    kind: str = "post",
    in_reply_to_object_id: str | None = None,
    conversation_root_object_id: str | None = None,
    source_kind: str = "content_create",
    logical_version: int = 1,
    source_message: MessageRef | None = None,
) -> PersistPublishResult:
    """Resolve only a complete, internally consistent durable publication graph."""
    published = session.scalar(
        select(PublishedActivityObject).where(
            PublishedActivityObject.object_id == object_id
        )
    )
    if published is None:
        raise PublicationConflictError(
            "mapping exists without canonical published object"
        )
    expected_published = (
        published.source_connector_id == mapping.source_connector_id
        and published.source_channel_surface_id == mapping.source_channel_surface_id
        and published.source_external_container_id == mapping.source_external_container_id
        and (source_message is None or (
            published.source_external_message_id == source_message.external_message_id
            and published.source_external_container_id == source_message.external_container_id
        ))
        and published.actor_url == actor_url
        and published.publication_context == publication_context
        and published.community_actor_url == community_actor_url
        and published.discord_guild_id == discord_guild_id
        and published.discord_channel_id == discord_channel_id
        and published.discord_message_id == discord_message_id
        and published.activity_id == activity_id
        and published.object_id == object_id
        and published.kind == kind
        and published.in_reply_to_object_id == in_reply_to_object_id
        and published.conversation_root_object_id
        == (conversation_root_object_id or object_id)
    )
    if not expected_published:
        raise PublicationConflictError(
            "mapping and canonical published object have incompatible identity"
        )
    if published_at is not None and (
        published.published_at.replace(tzinfo=None) != published_at.replace(tzinfo=None)
    ):
        raise PublicationConflictError(
            "published timestamp differs from deterministic source timestamp"
        )
    source = session.scalar(
        select(UserActorOutboundSource).where(
            UserActorOutboundSource.published_object_id == published.id
        )
    )
    if source is None:
        raise PublicationConflictError(
            "published object exists without follower delivery source"
        )
    expected_source = (
        source.activity_id == activity_id
        and source.published_object_id == published.id
        and source.source_kind == source_kind
        and source.logical_version == logical_version
        and source.ordering_key == f"content:{object_id}"
    )
    if not expected_source:
        raise PublicationConflictError(
            "outbound source has incompatible publication identity"
        )
    return PersistPublishResult(source.id, False)


def inspect_publish_artifacts(
    database: object,
    *,
    source_id: str,
    actor_url: str,
    publication_context: str,
    community_actor_url: str | None,
    discord_guild_id: int | None,
    discord_channel_id: int | None,
    discord_message_id: int | None,
    activity_id: str,
    object_id: str,
    published_at: datetime | None = None,
    kind: str = "post",
    in_reply_to_object_id: str | None = None,
    conversation_root_object_id: str | None = None,
    source_message: MessageRef | None = None,
) -> PersistPublishResult | None:
    """Return an exact durable duplicate or fail closed on partial/conflicting state."""
    expected = dict(
        source_id=source_id,
        actor_url=actor_url,
        publication_context=publication_context,
        community_actor_url=community_actor_url,
        discord_guild_id=discord_guild_id,
        discord_channel_id=discord_channel_id,
        discord_message_id=discord_message_id,
        activity_id=activity_id,
        object_id=object_id,
    )
    with database.session() as session:
        mapping = _mapping_by_source(session, source_id, source_message)
        if mapping is None:
            return None
        if not _matches(mapping, **expected) or not _scope_matches(
            session, mapping, discord_channel_id, source_message
        ):
            raise PublicationConflictError(
                "Discord source is already mapped to incompatible canonical identity"
            )
        return _existing_result(
            session,
            mapping,
            actor_url=actor_url,
            publication_context=publication_context,
            community_actor_url=community_actor_url,
            discord_guild_id=discord_guild_id,
            discord_channel_id=discord_channel_id,
            discord_message_id=discord_message_id,
            activity_id=activity_id,
            object_id=object_id,
            published_at=published_at,
            kind=kind,
            in_reply_to_object_id=in_reply_to_object_id,
            conversation_root_object_id=conversation_root_object_id,
            source_message=source_message,
        )


def persist_publish_artifacts(
    database: object,
    *,
    source_id: str,
    actor_username: str,
    actor_url: str,
    community_actor_url: str | None,
    activity_id: str,
    object_id: str,
    activity_json: dict,
    kind: str,
    title: str | None,
    body_markdown: str,
    in_reply_to_object_id: str | None,
    conversation_root_object_id: str | None = None,
    discord_channel_id: int | None,
    discord_message_id: int | None,
    publication_context: str = "community",
    discord_guild_id: int | None = None,
    published_at: datetime | None = None,
    source_message: MessageRef | None = None,
) -> PersistPublishResult:
    """Commit mapping, public object, and fixed follower audience atomically.

    Exact deterministic duplicates are idempotent. Any row that collides on a
    unique identity but disagrees on actor, context, source surface, or AP IDs
    is treated as corruption and fails closed.
    """
    expected = dict(
        source_id=source_id,
        actor_url=actor_url,
        publication_context=publication_context,
        community_actor_url=community_actor_url,
        discord_guild_id=discord_guild_id,
        discord_channel_id=discord_channel_id,
        discord_message_id=discord_message_id,
        activity_id=activity_id,
        object_id=object_id,
    )
    if source_message is not None and (
        source_message.connector_key != 'discord-primary'
        or source_message.external_message_id != source_id
        or (discord_message_id is not None and str(discord_message_id) != source_message.external_message_id)
    ):
        raise PublicationConflictError('Authored source is inconsistent with verified physical reference')
    try:
        with database.immediate_session() as session:
            user = session.scalar(select(User).where(User.actor_url == actor_url))
            if user is None or user.activitypub_username != actor_username:
                raise ValueError(
                    "canonical actor does not match a registered User Actor"
                )
            existing = _mapping_by_source(session, source_id, source_message)
            if existing is not None:
                if not _matches(existing, **expected) or not _scope_matches(
                    session, existing, discord_channel_id, source_message
                ):
                    raise PublicationConflictError(
                        "Discord source is already mapped to incompatible canonical identity"
                    )
                return _existing_result(
                    session,
                    existing,
                    actor_url=actor_url,
                    publication_context=publication_context,
                    community_actor_url=community_actor_url,
                    discord_guild_id=discord_guild_id,
                    discord_channel_id=discord_channel_id,
                    discord_message_id=discord_message_id,
                    activity_id=activity_id,
                    object_id=object_id,
                    published_at=published_at,
                    kind=kind,
                    in_reply_to_object_id=in_reply_to_object_id,
                    conversation_root_object_id=conversation_root_object_id,
                    source_message=source_message,
                )

            mapping = MessageMapping(
                **PlatformSurfaceRepository.discord_source_location(
                    session, discord_channel_id, discord_guild_id
                ),
                source_platform="discord",
                source_id=source_id,
                source_external_container_id=(
                    source_message.external_container_id if source_message is not None else None
                ),
                activity_id=activity_id,
                object_id=object_id,
                actor_url=actor_url,
                publication_context=publication_context,
                community_actor_url=community_actor_url,
                discord_guild_id=discord_guild_id,
                discord_channel_id=discord_channel_id,
                discord_message_id=discord_message_id,
            )
            published = PublishedActivityObject(
                **PlatformSurfaceRepository.discord_source_location(
                    session, discord_channel_id, discord_guild_id
                ),
                source_external_container_id=(
                    source_message.external_container_id if source_message is not None else None
                ),
                source_external_message_id=(
                    source_message.external_message_id if source_message is not None
                    else (str(discord_message_id) if discord_message_id is not None else None)
                ),
                actor_username=actor_username,
                actor_url=actor_url,
                publication_context=publication_context,
                community_actor_url=community_actor_url,
                activity_id=activity_id,
                object_id=object_id,
                kind=kind,
                title=title,
                body_markdown=body_markdown,
                in_reply_to_object_id=in_reply_to_object_id,
                conversation_root_object_id=conversation_root_object_id or object_id,
                discord_guild_id=discord_guild_id,
                discord_channel_id=discord_channel_id,
                discord_message_id=discord_message_id,
                content_revision=1,
                **({"published_at": published_at} if published_at is not None else {}),
            )
            session.add_all([mapping, published])
            session.flush()
            # The domain row/source physical receipt is equally part of this
            # publication graph, not an independent post-network commit.
            if publication_context == 'community' and source_message is not None:
                from .canonical_admission import admit_authored_comment, admit_authored_post
                if kind == 'post':
                    admit_authored_post(
                        session,published=published,source=source_message,
                        forum_channel_id=discord_channel_id,
                        source_thread_id=int(source_message.external_container_id),
                        source_starter_message_id=int(source_message.external_message_id),
                    )
                elif kind == 'comment':
                    admit_authored_comment(
                        session, published=published, source=source_message,
                        forum_channel_id=discord_channel_id,
                    )
                else:
                    raise ValueError('unsupported community publication kind')
            # Group-authored relay targets are part of the same canonical
            # transaction; a failed commit cannot leave an orphaned Announce.
            local_community = session.scalar(select(LocalCommunity).where(
                LocalCommunity.actor_url == community_actor_url,
                LocalCommunity.status == 'active',
            )) if community_actor_url else None
            if local_community is not None:
                database.local_community_authored_announces.stage_create(
                    session, published=published, community=local_community,
                    activity_json=activity_json,
                )
            followers = database.user_actor_followers.list_accepted_for_snapshot(
                session, user_id=user.id
            )
            # A current accepted remote Group Follow, bound to the original
            # forum claim, is independent evidence for direct User Actor
            # delivery. Bare targetInboxUrls cannot grant audience rights.
            direct_authorizations = []
            if community_actor_url and discord_channel_id is not None:
                current = session.scalar(select(ChannelCommunitySubscription).where(
                    ChannelCommunitySubscription.discord_channel_id == discord_channel_id,
                    ChannelCommunitySubscription.lemmy_community_actor_id == community_actor_url,
                    ChannelCommunitySubscription.status == 'accepted',
                ))
                if current is not None and current.community_inbox_url and current.follow_activity_id:
                    surface = session.get(ChannelSurface, mapping.source_channel_surface_id)
                    if surface is None or surface.remote_subscription_id != current.id:
                        raise PublicationConflictError('remote community lacks its current forum ownership')
                    direct_authorizations.append({
                        'schemaVersion': 1,
                        'contextKind': 'remote_community_publication',
                        'publishedObjectId': published.id,
                        'subscriptionId': current.id,
                        'recipientActorId': community_actor_url,
                        'followActivityId': current.follow_activity_id,
                        'targetEndpointUrl': current.community_inbox_url,
                        'sourceSurfaceId': surface.id,
                    })
            if publication_context == 'personal_feed' and in_reply_to_object_id:
                # The parent is authenticated inbound AP state, not a URL
                # supplied by an arbitrary Discord reply. A public reply may
                # address this exact parent without explicit AP to/cc.
                parent = session.scalar(select(InboundConversationObject).where(
                    InboundConversationObject.object_id == in_reply_to_object_id,
                    InboundConversationObject.publication_context == 'personal_feed',
                    InboundConversationObject.conversation_root_object_id == conversation_root_object_id,
                    InboundConversationObject.deleted_at.is_(None),
                ))
                root = session.scalar(select(PublishedActivityObject).where(
                    PublishedActivityObject.object_id == conversation_root_object_id,
                    PublishedActivityObject.publication_context == 'personal_feed',
                    PublishedActivityObject.deleted_at.is_(None),
                ))
                if parent is not None and root is not None:
                    remote_actor = session.scalar(select(RemoteActor).where(
                        RemoteActor.actor_url == parent.actor_id,
                    ))
                    if remote_actor is not None:
                        direct_authorizations.append({
                            'schemaVersion': 1,
                            'contextKind': 'personal_feed_reply',
                            'publishedObjectId': published.id,
                            'conversationRootObjectId': root.object_id,
                            'parentObjectId': parent.object_id,
                            'recipientActorId': parent.actor_id,
                            'targetEndpointUrl': remote_actor.shared_inbox_url or remote_actor.inbox_url,
                        })
            source = database.user_actor_deliveries.insert_ready_content_source(
                session,
                user_id=user.id,
                source_kind="content_create",
                logical_version=1,
                activity_id=activity_id,
                published_object_id=published.id,
                object_id=object_id,
                activity_json=activity_json,
                snapshot={
                    "schemaVersion": 1,
                    "objectId": object_id,
                    "activityType": "Create",
                    "contentRevision": 1,
                },
                followers=followers,
                direct_authorizations=direct_authorizations,
            )
            return PersistPublishResult(source.id, True)
    except IntegrityError:
        # The winner may commit after our preflight. The failed transaction must
        # be fully rolled back before a fresh session classifies the collision.
        with database.session() as session:
            if source_message is not None:
                source_condition = (
                    (MessageMapping.source_id == source_message.external_message_id)
                    & (MessageMapping.source_external_container_id == source_message.external_container_id)
                    & MessageMapping.source_connector_id.in_(
                        select(PlatformConnector.id).where(
                            PlatformConnector.connector_key == source_message.connector_key,
                            PlatformConnector.platform == 'discord',
                        )
                    )
                )
            else:
                # Legacy callers have no authenticated immediate container;
                # never guess among multiple typed sources after a race.
                source_condition = (
                    (MessageMapping.source_platform == 'discord')
                    & (MessageMapping.source_id == source_id)
                    & MessageMapping.source_external_container_id.is_(None)
                )
            mappings = list(session.scalars(
                select(MessageMapping).where(or_(
                    source_condition,
                    MessageMapping.activity_id == activity_id,
                    MessageMapping.object_id == object_id,
                ))
            ))
            if (
                len(mappings) == 1
                and _matches(mappings[0], **expected)
                and _scope_matches(session, mappings[0], discord_channel_id, source_message)
            ):
                return _existing_result(
                    session,
                    mappings[0],
                    actor_url=actor_url,
                    publication_context=publication_context,
                    community_actor_url=community_actor_url,
                    discord_guild_id=discord_guild_id,
                    discord_channel_id=discord_channel_id,
                    discord_message_id=discord_message_id,
                    activity_id=activity_id,
                    object_id=object_id,
                    published_at=published_at,
                    kind=kind,
                    in_reply_to_object_id=in_reply_to_object_id,
                    conversation_root_object_id=conversation_root_object_id,
                    source_message=source_message,
                )
        raise PublicationConflictError(
            "uniqueness collision did not resolve to the expected canonical publication"
        )
