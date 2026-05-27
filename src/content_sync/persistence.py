"""Shared persistence helpers for generic publish artifacts.

The two bridge modes keep different canonical mapping tables, but both persist
the same generic `message_mappings` and `published_activity_objects` rows for
dedup, loop suppression, and later AP object serving.
"""

from __future__ import annotations


def persist_publish_artifacts(
    database: object,
    *,
    source_id: str,
    actor_username: str,
    actor_url: str,
    community_actor_url: str,
    activity_id: str,
    object_id: str,
    kind: str,
    title: str | None,
    body_markdown: str,
    in_reply_to_object_id: str | None,
    discord_channel_id: int | None,
    discord_message_id: int | None,
) -> None:
    """Persist the generic publish rows shared by both bridge modes.

    The helper deliberately does not touch mode-specific tables such as thread
    groups or local-community thread/message rows. Those remain the owning
    runtime's responsibility.
    """
    database.message_mappings.create_message_mapping(
        source_platform="discord",
        source_id=source_id,
        activity_id=activity_id,
        object_id=object_id,
        actor_url=actor_url,
        community_actor_url=community_actor_url,
        discord_channel_id=discord_channel_id,
        discord_message_id=discord_message_id,
    )
    database.activitypub_objects.create_published_activity_object(
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
    )
