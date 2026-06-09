"""Database builders and seed helpers for bridge test scenarios.

These helpers intentionally create only the state a test asks for. They must
not auto-seed users, subscriptions, or mappings because many negative-path
tests rely on an empty world.
"""

from __future__ import annotations

from pathlib import Path

from src.db import Database
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN

COMMUNITY_ACTOR_URL = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"


def build_database(tmp_path: Path, name: str) -> Database:
    """Create one isolated SQLite database for a single test scenario."""
    database = Database(f"sqlite:///{tmp_path / name}")
    database.create_all()
    return database


def add_accepted_subscription(
    database: Database,
    *,
    channel_id: int = 100,
    guild_id: int = 1,
    community_actor_url: str = COMMUNITY_ACTOR_URL,
    community_name: str = "hackers",
    community_id: int = 42,
) -> None:
    """Insert one accepted subscription row for the shared community."""
    database.remote_subscriptions.create_subscription(
        discord_channel_id=channel_id,
        discord_guild_id=guild_id,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name=community_name,
        lemmy_community_id=community_id,
        community_handle=f"!{community_name}@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/activities/follow/{channel_id}",
        status="accepted",
    )


def add_registered_user(
    database: Database,
    *,
    discord_user_id: str = "123",
    username: str = "alice",
) -> None:
    """Insert one registered local user actor used by outbound publish scenarios."""
    actor_url = f"https://{BRIDGE_HOST_DOMAIN}/actors/{username}"
    database.users.create_user(
        discord_user_id=discord_user_id,
        activitypub_username=username,
        actor_url=actor_url,
        inbox_url=f"{actor_url}/inbox",
        outbox_url=f"{actor_url}/outbox",
        followers_url=f"{actor_url}/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )


def create_source_thread_group(
    database: Database,
    *,
    channel_id: int = 100,
    thread_id: int = 200,
    starter_message_id: int = 300,
    ap_object_id: str = f"https://{BRIDGE_HOST_DOMAIN}/objects/post/1",
    ap_activity_id: str = f"https://{BRIDGE_HOST_DOMAIN}/activities/create/post/1",
    community_actor_url: str = COMMUNITY_ACTOR_URL,
    include_post_link: bool = False,
) -> object:
    """Insert one source thread group and its source delivery row.

    Some legacy scenarios still need a `PostLink` because the publish service
    historically used it while the runtime moved to shared group tables.
    """
    if include_post_link:
        database.legacy_lemmy_mappings.create_post_link(
            lemmy_post_id=-thread_id,
            lemmy_post_ap_id=ap_object_id,
            discord_forum_channel_id=channel_id,
            discord_forum_thread_id=thread_id,
            discord_starter_message_id=starter_message_id,
            direction="discord_to_activitypub",
        )

    thread_group = database.discord_fanout_groups.create_thread_group(
        community_actor_id=community_actor_url,
        source_channel_id=channel_id,
        source_thread_id=thread_id,
        source_starter_message_id=starter_message_id,
        ap_activity_id=ap_activity_id,
        ap_object_id=ap_object_id,
    )
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=channel_id,
        discord_thread_id=thread_id,
        discord_starter_message_id=starter_message_id,
        role="source",
    )
    return thread_group


def add_thread_delivery(
    database: Database,
    *,
    thread_group_id: int,
    channel_id: int,
    thread_id: int,
    starter_message_id: int,
    role: str,
) -> None:
    """Insert one thread delivery row with the requested role."""
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group_id,
        discord_channel_id=channel_id,
        discord_thread_id=thread_id,
        discord_starter_message_id=starter_message_id,
        role=role,
    )


def create_inbound_thread_group(
    database: Database,
    *,
    ap_object_id: str,
    channel_id: int = 100,
    thread_id: int = 200,
    starter_message_id: int = 300,
    community_actor_url: str = COMMUNITY_ACTOR_URL,
) -> object:
    """Insert one inbound thread group with a single inbound delivery row."""
    thread_group = database.discord_fanout_groups.create_thread_group(
        community_actor_id=community_actor_url,
        source_channel_id=None,
        source_thread_id=None,
        source_starter_message_id=None,
        ap_object_id=ap_object_id,
    )
    add_thread_delivery(
        database,
        thread_group_id=thread_group.id,
        channel_id=channel_id,
        thread_id=thread_id,
        starter_message_id=starter_message_id,
        role="inbound",
    )
    return thread_group

