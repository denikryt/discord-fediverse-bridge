"""Atomic persistence for canonical Discord-originated ActivityPub creates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..models import (
    BridgeActorFollow,
    ChannelCommunitySubscription,
    MessageMapping,
    PublishedActivityObject,
    User,
    UserActorOutboundSource,
)


class PublicationConflictError(RuntimeError):
    """Signal that durable identity conflicts with an attempted publication."""


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
    direct_group_recipients: list[dict] | None = None,
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
        published.actor_url == actor_url
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
    if direct_group_recipients is not None:
        import json

        snapshot = json.loads(source.snapshot_json)
        if snapshot.get("directGroupRecipients") != direct_group_recipients:
            raise PublicationConflictError(
                "existing Create has incompatible immutable direct Group recipients"
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
    direct_group_recipients: list[dict] | None = None,
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
        mapping = session.scalar(
            select(MessageMapping).where(
                MessageMapping.source_platform == "discord",
                MessageMapping.source_id == source_id,
            )
        )
        if mapping is None:
            return None
        if not _matches(mapping, **expected):
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
            direct_group_recipients=direct_group_recipients,
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
    direct_target_inbox_urls: list[str] | None = None,
    direct_group_recipients: list[dict] | None = None,
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
    try:
        with database.immediate_session() as session:
            user = session.scalar(select(User).where(User.actor_url == actor_url))
            if user is None or user.activitypub_username != actor_username:
                raise ValueError(
                    "canonical actor does not match a registered User Actor"
                )
            if direct_group_recipients is not None:
                # Recheck Follow, binding and inbox under the *same* SQLite writer
                # transaction that commits the immutable Create and target.
                for recipient in direct_group_recipients:
                    follow = session.scalar(select(BridgeActorFollow).where(
                        BridgeActorFollow.community_actor_id == recipient["actorId"],
                        BridgeActorFollow.follow_activity_id == recipient["followActivityId"],
                        BridgeActorFollow.community_inbox_url == recipient["inboxUrl"],
                        BridgeActorFollow.remote_software_profile == recipient["softwareProfile"],
                        BridgeActorFollow.status == "accepted",
                        BridgeActorFollow.cancel_requested.is_(False),
                        BridgeActorFollow.follow_dispatch_state == "sent",
                    ))
                    binding = session.scalar(select(ChannelCommunitySubscription).where(
                        ChannelCommunitySubscription.discord_channel_id == recipient["discordChannelId"],
                        ChannelCommunitySubscription.lemmy_community_actor_id == recipient["actorId"],
                        ChannelCommunitySubscription.follow_activity_id == recipient["followActivityId"],
                        ChannelCommunitySubscription.community_inbox_url == recipient["inboxUrl"],
                        ChannelCommunitySubscription.status == "accepted",
                    ))
                    if follow is None or binding is None or (
                        binding.discord_guild_id != recipient["discordGuildId"]
                    ):
                        raise PublicationConflictError(
                            "direct Group recipient is no longer backed by the accepted Follow generation"
                        )
            existing = session.scalar(
                select(MessageMapping).where(
                    MessageMapping.source_platform == "discord",
                    MessageMapping.source_id == source_id,
                )
            )
            if existing is not None:
                if not _matches(existing, **expected):
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
                    direct_group_recipients=direct_group_recipients,
                )

            mapping = MessageMapping(
                source_platform="discord",
                source_id=source_id,
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
            followers = database.user_actor_followers.list_accepted_for_snapshot(
                session, user_id=user.id
            )
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
                    **({"directGroupRecipients": direct_group_recipients} if direct_group_recipients is not None else {}),
                },
                followers=followers,
                direct_target_inbox_urls=direct_target_inbox_urls,
                direct_group_recipients=direct_group_recipients,
            )
            return PersistPublishResult(source.id, True)
    except IntegrityError:
        # The winner may commit after our preflight. The failed transaction must
        # be fully rolled back before a fresh session classifies the collision.
        with database.session() as session:
            mappings = list(
                session.scalars(
                    select(MessageMapping).where(
                        or_(
                            (MessageMapping.source_platform == "discord")
                            & (MessageMapping.source_id == source_id),
                            MessageMapping.activity_id == activity_id,
                            MessageMapping.object_id == object_id,
                            MessageMapping.discord_message_id == discord_message_id,
                        )
                    )
                )
            )
            if len(mappings) == 1 and _matches(mappings[0], **expected):
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
                    direct_group_recipients=direct_group_recipients,
                )
        raise PublicationConflictError(
            "uniqueness collision did not resolve to the expected canonical publication"
        )
