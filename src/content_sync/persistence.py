"""Atomic persistence for canonical Discord-originated ActivityPub creates."""

from __future__ import annotations

from sqlalchemy import select

from ..models import MessageMapping, PublishedActivityObject, User


def persist_publish_artifacts(
    database: object,
    *,
    source_id: str,
    actor_username: str,
    actor_url: str,
    community_actor_url: str,
    activity_id: str,
    object_id: str,
    activity_json: dict,
    kind: str,
    title: str | None,
    body_markdown: str,
    in_reply_to_object_id: str | None,
    discord_channel_id: int | None,
    discord_message_id: int | None,
) -> int:
    """Commit mapping, public object, and fixed follower audience atomically.

    The gateway has already delivered to the existing community path. This
    transaction makes the exact canonical activity durable for independent
    follower retry without regenerating IDs or JSON.
    """
    with database.immediate_session() as session:
        user = session.scalar(select(User).where(User.actor_url == actor_url))
        if user is None or user.activitypub_username != actor_username:
            raise ValueError("canonical actor does not match a registered User Actor")
        mapping = MessageMapping(
            source_platform="discord",
            source_id=source_id,
            activity_id=activity_id,
            object_id=object_id,
            actor_url=actor_url,
            community_actor_url=community_actor_url,
            discord_channel_id=discord_channel_id,
            discord_message_id=discord_message_id,
        )
        published = PublishedActivityObject(
            actor_username=actor_username,
            actor_url=actor_url,
            community_actor_url=community_actor_url,
            activity_id=activity_id,
            object_id=object_id,
            kind=kind,
            title=title,
            body_markdown=body_markdown,
            in_reply_to_object_id=in_reply_to_object_id,
            discord_channel_id=discord_channel_id,
            discord_message_id=discord_message_id,
            content_revision=1,
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
            },
            followers=followers,
        )
        return source.id
