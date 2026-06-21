"""Behavior coverage for Stage 8 personal-feed conversation persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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
            actor_url="https://bridge.example/actors/users/alice",
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
        attributed_to_actor_id="https://remote.example/actors/users/bob",
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
        activity_actor_id="https://remote.example/actors/users/bob",
        attributed_to_actor_id="https://remote.example/actors/users/bob",
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
        activity_actor_id="https://remote.example/actors/users/bob",
        attributed_to_actor_id="https://remote.example/actors/users/bob",
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
        activity_actor_id="https://remote.example/actors/users/bob",
        attributed_to_actor_id="https://remote.example/actors/users/bob",
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
        activity_actor_id="https://remote.example/actors/users/bob",
        attributed_to_actor_id="https://remote.example/actors/users/bob",
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
        activity_actor_id="https://remote.example/actors/users/bob",
        attributed_to_actor_id="https://remote.example/actors/users/bob",
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
        activity_actor_id="https://remote.example/actors/users/bob",
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
        activity_actor_id="https://remote.example/actors/users/bob",
        attributed_to_actor_id="https://remote.example/actors/users/bob",
        actor_name="Bob",
        content_markdown="transient",
        published_at=datetime.now(timezone.utc),
        source_activity={"type": "Create"},
        destination_channel_id=10,
        parent_known=True,
    )

    database.personal_feed_conversations.admit_delete(
        object_id="https://remote.example/o/1",
        activity_actor_id="https://remote.example/actors/users/bob",
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
        actor_id="https://remote.example/actors/users/bob",
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


@pytest.mark.asyncio
async def test_worker_stop_cancels_blocked_discord_call_without_spending_attempt(
    tmp_path,
) -> None:
    """Shutdown interrupts a stalled Discord await and leaves the claim restartable."""
    import asyncio
    from types import SimpleNamespace

    from src.models import InboundDiscordDelivery, User
    from src.personal_feed_discord_delivery_worker import (
        PersonalFeedDiscordDeliveryWorker,
    )
    from src.personal_feeds import PersonalFeedService

    database = Database(f"sqlite:///{tmp_path / 'cancel.sqlite3'}")
    database.create_all()
    database.migrate()
    with database.session() as session:
        session.add(
            User(
                discord_user_id="10",
                activitypub_username="alice",
                actor_url="https://bridge.example/actors/users/alice",
                inbox_url="https://bridge.example/actors/users/alice/inbox",
                outbox_url="https://bridge.example/actors/users/alice/outbox",
                followers_url="https://bridge.example/actors/users/alice/followers",
                public_key_pem="public",
                private_key_pem="private",
            )
        )
    service = PersonalFeedService(database)
    service.bind_or_move("10", 1, 10)
    _root(database)
    created = database.personal_feed_conversations.admit_create(
        activity_id="https://remote.example/a/blocked",
        object_id="https://remote.example/o/blocked",
        root_object_id="https://bridge.example/o/root",
        parent_object_id="https://bridge.example/o/root",
        activity_actor_id="https://remote.example/actors/users/bob",
        attributed_to_actor_id="https://remote.example/actors/users/bob",
        actor_name="Bob",
        content_markdown="blocked",
        published_at=datetime.now(timezone.utc),
        source_activity={"type": "Create"},
        destination_channel_id=10,
        parent_known=True,
    )
    entered = asyncio.Event()
    never = asyncio.Event()

    async def blocked_fetch(_channel_id: int) -> object:
        entered.set()
        await never.wait()
        raise AssertionError("unreachable")

    bot = SimpleNamespace(get_channel=lambda _id: None, fetch_channel=blocked_fetch)
    worker = PersonalFeedDiscordDeliveryWorker(
        database=database, bot=bot, personal_feed_service=service
    )
    worker.start()
    await asyncio.wait_for(entered.wait(), timeout=1)

    await asyncio.wait_for(worker.stop(), timeout=1)

    with database.session() as session:
        delivery = session.get(InboundDiscordDelivery, created.delivery_id)
        assert delivery.state == "ready"
        assert delivery.attempt_count == 0


@pytest.mark.asyncio
async def test_worker_creates_deterministic_thread_and_sends_without_mentions(
    tmp_path,
) -> None:
    """A retained remote reply creates the root thread once and records one Discord message."""
    from types import SimpleNamespace

    import discord

    from src.models import InboundDiscordDelivery, User
    from src.personal_feed_discord_delivery_worker import (
        PersonalFeedDiscordDeliveryWorker,
    )
    from src.personal_feeds import PersonalFeedService

    database = Database(f"sqlite:///{tmp_path / 'worker.sqlite3'}")
    database.create_all()
    database.migrate()
    with database.session() as session:
        session.add(
            User(
                discord_user_id="10",
                activitypub_username="alice",
                actor_url="https://bridge.example/actors/users/alice",
                inbox_url="https://bridge.example/actors/users/alice/inbox",
                outbox_url="https://bridge.example/actors/users/alice/outbox",
                followers_url="https://bridge.example/actors/users/alice/followers",
                public_key_pem="public",
                private_key_pem="private",
            )
        )
    service = PersonalFeedService(database)
    service.bind_or_move("10", 1, 10)
    _root(database)
    created = database.personal_feed_conversations.admit_create(
        activity_id="https://remote.example/a/1",
        object_id="https://remote.example/o/1",
        root_object_id="https://bridge.example/o/root",
        parent_object_id="https://bridge.example/o/root",
        activity_actor_id="https://remote.example/actors/users/bob",
        attributed_to_actor_id="https://remote.example/actors/users/bob",
        actor_name="Bob",
        content_markdown="hello @everyone",
        published_at=datetime.now(timezone.utc),
        source_activity={"type": "Create"},
        destination_channel_id=10,
        parent_known=True,
    )
    sent: list[tuple[str, object]] = []

    async def send(content: str, *, allowed_mentions: object) -> object:
        sent.append((content, allowed_mentions))
        return SimpleNamespace(id=777)

    thread = SimpleNamespace(send=send)

    async def create_thread(*, name: str) -> object:
        assert "@" not in name
        return thread

    root_message = SimpleNamespace(create_thread=create_thread)
    channel = SimpleNamespace(fetch_message=lambda _id: _async_value(root_message))
    bot = SimpleNamespace(
        get_channel=lambda _id: None,
        fetch_channel=lambda _id: _async_value(channel),
    )
    worker = PersonalFeedDiscordDeliveryWorker(
        database=database, bot=bot, personal_feed_service=service
    )

    claim = database.personal_feed_conversations.claim_due()[0]
    await worker._deliver(claim)

    assert sent and sent[0][0].endswith("hello @everyone")
    assert isinstance(sent[0][1], discord.AllowedMentions)
    with database.session() as session:
        delivery = session.get(InboundDiscordDelivery, created.delivery_id)
        assert delivery.state == "delivered"
        assert delivery.discord_message_id == 777
        assert delivery.attempt_count == 1


def _async_value(value):
    """Return one value through an awaitable outer-boundary fake."""

    async def inner():
        return value

    return inner()


@pytest.mark.asyncio
async def test_worker_rechecks_binding_after_network_lookup_before_discord_send(
    tmp_path,
) -> None:
    """A move during Discord lookup suppresses the side effect without spending retry budget."""
    from types import SimpleNamespace

    from src.models import InboundDiscordDelivery, User
    from src.personal_feed_discord_delivery_worker import (
        PersonalFeedDiscordDeliveryWorker,
    )

    database = Database(f"sqlite:///{tmp_path / 'stale-race.sqlite3'}")
    database.create_all()
    database.migrate()
    with database.session() as session:
        session.add(
            User(
                discord_user_id="10",
                activitypub_username="alice",
                actor_url="https://bridge.example/actors/users/alice",
                inbox_url="https://bridge.example/actors/users/alice/inbox",
                outbox_url="https://bridge.example/actors/users/alice/outbox",
                followers_url="https://bridge.example/actors/users/alice/followers",
                public_key_pem="public",
                private_key_pem="private",
            )
        )
    _root(database)
    created = database.personal_feed_conversations.admit_create(
        activity_id="https://remote.example/a/race",
        object_id="https://remote.example/o/race",
        root_object_id="https://bridge.example/o/root",
        parent_object_id="https://bridge.example/o/root",
        activity_actor_id="https://remote.example/actors/users/bob",
        attributed_to_actor_id="https://remote.example/actors/users/bob",
        actor_name="Bob",
        content_markdown="reply",
        published_at=datetime.now(timezone.utc),
        source_activity={"type": "Create"},
        destination_channel_id=10,
        parent_known=True,
    )
    checks = iter((True, False))
    service = SimpleNamespace(
        is_active_personal_feed_channel=lambda _user_id, _channel_id: next(checks)
    )
    send_calls: list[str] = []

    async def fetch_message(_message_id: int) -> object:
        return SimpleNamespace(
            create_thread=lambda **_kwargs: _async_value(
                SimpleNamespace(
                    send=lambda *_args, **_kwargs: send_calls.append("send")
                )
            )
        )

    channel = SimpleNamespace(fetch_message=fetch_message)
    bot = SimpleNamespace(
        get_channel=lambda _id: None,
        fetch_channel=lambda _id: _async_value(channel),
    )
    worker = PersonalFeedDiscordDeliveryWorker(
        database=database, bot=bot, personal_feed_service=service
    )

    await worker._deliver(database.personal_feed_conversations.claim_due()[0])

    assert send_calls == []
    with database.session() as session:
        delivery = session.get(InboundDiscordDelivery, created.delivery_id)
        assert delivery.state == "suppressed"
        assert delivery.attempt_count == 0
