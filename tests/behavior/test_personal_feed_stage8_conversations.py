"""Behavior coverage for Stage 8 personal-feed conversation persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.db import Database
from src.models import (
    InboundConversationObject,
    InboundDiscordDelivery,
    PublishedActivityObject,
)


def _database() -> Database:
    """Create one migrated in-memory database for a concrete Stage 8 scenario."""
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_all()
    database.migrate()
    return database


def _root(database: Database) -> PublishedActivityObject:
    """Persist one Stage 7 personal-feed root used by conversation actions."""
    with database.session() as session:
        root = PublishedActivityObject(
            actor_username="alice",
            actor_url="https://bridge.example/users/alice",
            publication_context="personal_feed",
            community_actor_url=None,
            activity_id="https://bridge.example/a/root",
            object_id="https://bridge.example/o/root",
            kind="post",
            title=None,
            body_markdown="root",
            in_reply_to_object_id=None,
            conversation_root_object_id="https://bridge.example/o/root",
            discord_guild_id=1,
            discord_channel_id=10,
            discord_message_id=100,
        )
        session.add(root)
        session.flush()
        return root


def test_manual_thread_adoption_uses_discord_deterministic_identity() -> None:
    """A source message can own only Discord's same-snowflake public thread."""
    database = _database()
    _root(database)

    mapping = database.personal_feed_conversations.adopt_thread(
        root_object_id="https://bridge.example/o/root",
        channel_id=10,
        root_message_id=100,
        thread_id=100,
    )

    assert mapping.thread_id == 100
    assert (
        database.personal_feed_conversations.adopt_thread(
            root_object_id="https://bridge.example/o/root",
            channel_id=10,
            root_message_id=100,
            thread_id=100,
        ).id
        == mapping.id
    )


def test_create_rejects_actor_attributed_to_mismatch_before_storage() -> None:
    """An actor cannot create a canonical comment attributed to another actor."""
    database = _database()
    _root(database)

    result = database.personal_feed_conversations.admit_create(
        activity_id="https://remote.example/a/1",
        object_id="https://remote.example/o/1",
        root_object_id="https://bridge.example/o/root",
        parent_object_id="https://bridge.example/o/root",
        activity_actor_id="https://remote.example/users/mallory",
        attributed_to_actor_id="https://remote.example/users/bob",
        actor_name="Mallory",
        content_markdown="forged",
        published_at=datetime.now(timezone.utc),
        source_activity={"type": "Create"},
        destination_channel_id=10,
        parent_known=True,
    )

    assert result.status == "actor_mismatch"
    with database.session() as session:
        assert session.query(InboundConversationObject).count() == 0
        assert session.query(InboundDiscordDelivery).count() == 0


def test_same_origin_wrong_actor_cannot_update_or_delete_comment() -> None:
    """Object ownership is exact actor identity, not merely matching origin."""
    database = _database()
    _root(database)
    created = database.personal_feed_conversations.admit_create(
        activity_id="https://remote.example/a/1",
        object_id="https://remote.example/o/1",
        root_object_id="https://bridge.example/o/root",
        parent_object_id="https://bridge.example/o/root",
        activity_actor_id="https://remote.example/users/bob",
        attributed_to_actor_id="https://remote.example/users/bob",
        actor_name="Bob",
        content_markdown="original",
        published_at=datetime.now(timezone.utc),
        source_activity={"type": "Create"},
        destination_channel_id=10,
        parent_known=True,
    )
    assert created.status == "created"

    update = database.personal_feed_conversations.admit_update(
        object_id="https://remote.example/o/1",
        activity_actor_id="https://remote.example/users/mallory",
        attributed_to_actor_id="https://remote.example/users/mallory",
        content_markdown="forged",
        updated_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        source_activity={"type": "Update"},
    )
    delete = database.personal_feed_conversations.admit_delete(
        object_id="https://remote.example/o/1",
        activity_actor_id="https://remote.example/users/mallory",
    )

    assert update.status == "actor_mismatch"
    assert delete.status == "actor_mismatch"
    with database.session() as session:
        row = session.query(InboundConversationObject).one()
        assert row.content_markdown == "original"
        assert row.deleted_at is None


def test_pending_parent_does_not_consume_discord_attempt_budget() -> None:
    """Dependency waiting remains distinct from failed Discord side effects."""
    database = _database()
    _root(database)
    created = database.personal_feed_conversations.admit_create(
        activity_id="https://remote.example/a/child",
        object_id="https://remote.example/o/child",
        root_object_id="https://bridge.example/o/root",
        parent_object_id="https://remote.example/o/parent",
        activity_actor_id="https://remote.example/users/bob",
        attributed_to_actor_id="https://remote.example/users/bob",
        actor_name="Bob",
        content_markdown="child",
        published_at=datetime.now(timezone.utc),
        source_activity={"type": "Create"},
        destination_channel_id=10,
        parent_known=False,
    )
    assert created.status == "created"

    with database.session() as session:
        delivery = session.get(InboundDiscordDelivery, created.delivery_id)
        assert delivery.state == "pending_parent"
        assert delivery.attempt_count == 0

    assert (
        database.personal_feed_conversations.wake_children(
            "https://remote.example/o/parent"
        )
        == 1
    )
    with database.session() as session:
        delivery = session.get(InboundDiscordDelivery, created.delivery_id)
        assert delivery.state == "ready"
        assert delivery.attempt_count == 0


def test_only_actual_discord_calls_exhaust_three_attempt_budget() -> None:
    """Three external failures become terminal without deleting canonical state."""
    database = _database()
    _root(database)
    created = database.personal_feed_conversations.admit_create(
        activity_id="https://remote.example/a/1",
        object_id="https://remote.example/o/1",
        root_object_id="https://bridge.example/o/root",
        parent_object_id="https://bridge.example/o/root",
        activity_actor_id="https://remote.example/users/bob",
        attributed_to_actor_id="https://remote.example/users/bob",
        actor_name="Bob",
        content_markdown="reply",
        published_at=datetime.now(timezone.utc),
        source_activity={"type": "Create"},
        destination_channel_id=10,
        parent_known=True,
    )

    assert (
        database.personal_feed_conversations.record_discord_attempt(
            created.delivery_id, success=False, error="one"
        )
        == "ready"
    )
    assert (
        database.personal_feed_conversations.record_discord_attempt(
            created.delivery_id, success=False, error="two"
        )
        == "ready"
    )
    assert (
        database.personal_feed_conversations.record_discord_attempt(
            created.delivery_id, success=False, error="three"
        )
        == "failed"
    )
    with database.session() as session:
        delivery = session.get(InboundDiscordDelivery, created.delivery_id)
        assert delivery.attempt_count == 3
        assert session.get(InboundConversationObject, created.object_id) is not None


def test_delivered_update_reuses_discord_message_and_delete_becomes_due() -> None:
    """Remote mutations reconcile the existing Discord message instead of appending."""
    database = _database()
    _root(database)
    created = database.personal_feed_conversations.admit_create(
        activity_id="https://remote.example/a/1",
        object_id="https://remote.example/o/1",
        root_object_id="https://bridge.example/o/root",
        parent_object_id="https://bridge.example/o/root",
        activity_actor_id="https://remote.example/users/bob",
        attributed_to_actor_id="https://remote.example/users/bob",
        actor_name="Bob",
        content_markdown="original",
        published_at=datetime.now(timezone.utc),
        source_activity={"type": "Create"},
        destination_channel_id=10,
        parent_known=True,
    )
    database.personal_feed_conversations.record_discord_attempt(
        created.delivery_id, success=True, discord_message_id=501
    )

    updated = database.personal_feed_conversations.admit_update(
        object_id="https://remote.example/o/1",
        activity_actor_id="https://remote.example/users/bob",
        attributed_to_actor_id="https://remote.example/users/bob",
        content_markdown="edited",
        updated_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        source_activity={"type": "Update"},
    )
    assert updated.status == "accepted"
    claim = database.personal_feed_conversations.claim_due()[0]
    assert claim.discord_message_id == 501
    assert claim.deleted is False
    database.personal_feed_conversations.record_discord_attempt(
        claim.delivery_id, success=True, discord_message_id=501
    )

    deleted = database.personal_feed_conversations.admit_delete(
        object_id="https://remote.example/o/1",
        activity_actor_id="https://remote.example/users/bob",
    )
    assert deleted.status == "accepted"
    claim = database.personal_feed_conversations.claim_due()[0]
    assert claim.discord_message_id == 501
    assert claim.deleted is True


def test_delete_before_first_discord_render_is_suppressed_without_attempt() -> None:
    """A terminal Delete cannot create a thread merely to remove an unseen comment."""
    database = _database()
    _root(database)
    created = database.personal_feed_conversations.admit_create(
        activity_id="https://remote.example/a/1",
        object_id="https://remote.example/o/1",
        root_object_id="https://bridge.example/o/root",
        parent_object_id="https://bridge.example/o/root",
        activity_actor_id="https://remote.example/users/bob",
        attributed_to_actor_id="https://remote.example/users/bob",
        actor_name="Bob",
        content_markdown="transient",
        published_at=datetime.now(timezone.utc),
        source_activity={"type": "Create"},
        destination_channel_id=10,
        parent_known=True,
    )

    database.personal_feed_conversations.admit_delete(
        object_id="https://remote.example/o/1",
        activity_actor_id="https://remote.example/users/bob",
    )

    assert database.personal_feed_conversations.claim_due() == []
    with database.session() as session:
        delivery = session.get(InboundDiscordDelivery, created.delivery_id)
        assert delivery.state == "suppressed"
        assert delivery.attempt_count == 0


def test_community_comment_is_retained_without_personal_feed_delivery_state() -> None:
    """Existing community handlers can persist canonical history before Discord fanout."""
    database = _database()

    result = database.personal_feed_conversations.retain_community_create(
        activity_id="https://remote.example/a/community-comment",
        object_id="https://remote.example/o/community-comment",
        root_object_id="https://remote.example/o/community-post",
        parent_object_id="https://remote.example/o/community-post",
        actor_id="https://remote.example/users/bob",
        actor_name="Bob",
        content_markdown="retained",
        published_at=datetime.now(timezone.utc),
        source_activity={"type": "Create"},
    )

    assert result.status == "created"
    with database.session() as session:
        row = session.get(InboundConversationObject, result.object_id)
        assert row.publication_context == "community"
        assert session.query(InboundDiscordDelivery).count() == 0
