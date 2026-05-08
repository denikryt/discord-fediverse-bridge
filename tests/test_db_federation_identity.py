"""Repository scenarios for the Stage 2 federation identity schema."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from src.db import Database
from tests_constants import (
    BRIDGE_HOST_DOMAIN,
    LEMMY_WORLD_DOMAIN,
)


def _database(tmp_path: Path) -> Database:
    """Create a real SQLite-backed repository for scenario-style DB tests."""
    database = Database(f"sqlite:///{tmp_path / 'bridge-stage2.db'}")
    database.create_all()
    return database


def test_create_all_builds_stage_2_federation_tables(tmp_path: Path) -> None:
    """A clean Stage 2 startup should create all identity and dedup tables."""
    database = _database(tmp_path)

    table_names = set(inspect(database.engine).get_table_names())

    assert "users" in table_names
    assert "message_mappings" in table_names
    assert "published_activity_objects" in table_names
    assert "remote_actors" in table_names
    assert "channel_community_subscriptions" in table_names
    assert "post_links" in table_names
    assert "comment_links" in table_names


def test_registered_user_can_be_created_and_loaded_by_all_identity_keys(tmp_path: Path) -> None:
    """User ownership records must resolve by Discord ID, username, and actor URL."""
    database = _database(tmp_path)
    actor_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice"

    created = database.create_user(
        discord_user_id="1234567890",
        activitypub_username="alice",
        actor_url=actor_url,
        inbox_url=f"{actor_url}/inbox",
        outbox_url=f"{actor_url}/outbox",
        followers_url=f"{actor_url}/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )

    by_discord_id = database.get_user_by_discord_user_id("1234567890")
    by_username = database.get_user_by_activitypub_username("alice")
    by_actor_url = database.get_user_by_actor_url(actor_url)

    assert created.activitypub_username == "alice"
    assert by_discord_id is not None
    assert by_discord_id.id == created.id
    assert by_username is not None
    assert by_username.id == created.id
    assert by_actor_url is not None
    assert by_actor_url.private_key_pem == "private-key"


def test_subscription_follow_state_is_persisted_for_existing_subscription(tmp_path: Path) -> None:
    """Subscription rows must carry federation follow metadata for later stages."""
    database = _database(tmp_path)
    community_actor_url = f"https://{LEMMY_WORLD_DOMAIN}/c/hackers"
    database.create_subscription(
        discord_channel_id=777,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_WORLD_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/activities/follow-1",
        status="accepted",
    )

    subscription = database.get_subscription_by_channel(777)

    assert subscription is not None
    assert subscription.community_handle == f"!hackers@{LEMMY_WORLD_DOMAIN}"
    assert subscription.community_inbox_url == f"{community_actor_url}/inbox"
    assert subscription.follow_activity_id == f"https://{BRIDGE_HOST_DOMAIN}/activities/follow-1"
    assert subscription.status == "accepted"

    database.update_subscription_follow_state(
        discord_channel_id=777,
        community_inbox_url=f"https://{LEMMY_WORLD_DOMAIN}/inbox/updated",
        follow_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/activities/follow-2",
        status="pending",
    )

    updated = database.get_subscription_by_channel(777)

    assert updated is not None
    assert updated.community_inbox_url == f"https://{LEMMY_WORLD_DOMAIN}/inbox/updated"
    assert updated.follow_activity_id == f"https://{BRIDGE_HOST_DOMAIN}/activities/follow-2"
    assert updated.status == "pending"


def test_message_mapping_lookup_supports_dedup_keys_used_by_bridge(tmp_path: Path) -> None:
    """Generic message mappings must be queryable by both activity and object IDs."""
    database = _database(tmp_path)
    actor_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice"
    activity_id = f"https://{BRIDGE_HOST_DOMAIN}/activities/555"
    object_id = f"https://{BRIDGE_HOST_DOMAIN}/objects/555"

    created = database.create_message_mapping(
        source_platform="discord",
        source_id="discord-message-555",
        activity_id=activity_id,
        object_id=object_id,
        actor_url=actor_url,
        community_actor_url=f"https://{LEMMY_WORLD_DOMAIN}/c/hackers",
        discord_channel_id=123,
        discord_message_id=555,
    )

    by_activity = database.get_message_mapping_by_activity_id(activity_id)
    by_object = database.get_message_mapping_by_object_id(object_id)
    by_discord_message = database.get_message_mapping_by_discord_message_id(555)

    assert created.source_platform == "discord"
    assert by_activity is not None
    assert by_activity.id == created.id
    assert by_object is not None
    assert by_object.id == created.id
    assert by_discord_message is not None
    assert by_discord_message.source_id == "discord-message-555"


def test_remote_actor_upsert_refreshes_existing_record_without_duplicates(tmp_path: Path) -> None:
    """Remote actor cache rows should update in place when the actor is refetched."""
    database = _database(tmp_path)
    actor_url = f"https://{LEMMY_WORLD_DOMAIN}/u/alice"

    first = database.upsert_remote_actor(
        actor_url=actor_url,
        preferred_username="alice",
        inbox_url=f"{actor_url}/inbox",
        shared_inbox_url=f"https://{LEMMY_WORLD_DOMAIN}/inbox",
        public_key_pem="public-key-v1",
    )
    second = database.upsert_remote_actor(
        actor_url=actor_url,
        preferred_username="alice-renamed",
        inbox_url=f"{actor_url}/inbox-v2",
        shared_inbox_url=f"https://{LEMMY_WORLD_DOMAIN}/inbox-v2",
        public_key_pem="public-key-v2",
    )

    loaded = database.get_remote_actor_by_actor_url(actor_url)

    assert first.id == second.id
    assert loaded is not None
    assert loaded.id == first.id
    assert loaded.preferred_username == "alice-renamed"
    assert loaded.inbox_url == f"{actor_url}/inbox-v2"
    assert loaded.shared_inbox_url == f"https://{LEMMY_WORLD_DOMAIN}/inbox-v2"
    assert loaded.public_key_pem == "public-key-v2"


def test_published_activity_objects_can_be_loaded_by_canonical_object_id(
    tmp_path: Path,
) -> None:
    """Published AP objects must stay addressable by their canonical object URL."""
    database = _database(tmp_path)
    object_id = f"https://{BRIDGE_HOST_DOMAIN}/users/alice/post/555"

    created = database.create_published_activity_object(
        actor_username="alice",
        actor_url=f"https://{BRIDGE_HOST_DOMAIN}/users/alice",
        community_actor_url=f"https://{LEMMY_WORLD_DOMAIN}/c/hackers",
        activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/post/555",
        object_id=object_id,
        kind="post",
        title="Stored bridge post",
        body_markdown="hello from discord",
        in_reply_to_object_id=None,
        discord_channel_id=123,
        discord_message_id=555,
    )

    loaded = database.get_published_activity_object_by_object_id(object_id)

    assert loaded is not None
    assert loaded.id == created.id
    assert loaded.kind == "post"
    assert loaded.title == "Stored bridge post"
