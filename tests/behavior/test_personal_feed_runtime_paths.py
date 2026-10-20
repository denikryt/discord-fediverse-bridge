"""Runtime-path coverage for personal-feed conversations personal-feed conversations."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activitypub_handlers import dispatch_activitypub_event
from src.db import Database
from src.db.repositories.platform_surfaces import PlatformSurfaceRepository
from src.discord_bot import BridgeBot
from src.discord_event_router import DiscordEventRouter
from src.discord_message_mutations import DiscordMessageMutationHandler
from src.runtime_ports import DiscordMutationContext
from src.fedify_gateway_client import RenderPersonalFeedResult
from src.models import MessageMapping, PublishedActivityObject, User
from src.personal_feed_conversations import PersonalFeedConversationRuntime
from src.personal_feeds import PersonalFeedService


class _Policy:
    """Allow all federation targets in personal-feed raw mutation tests."""

    def federation_decision(self, _target: str) -> object:
        """Return an allowed decision object."""
        return SimpleNamespace(allowed=True)


class _DeliveryService:
    """Accept raw personal-feed mutation submissions in handler tests."""

    async def submit_update(self, **_kwargs: object) -> str:
        """Accept one Update admission."""
        return "accepted"

    async def submit_delete(self, **_kwargs: object) -> str:
        """Accept one Delete admission."""
        return "accepted"


def _mutation_context() -> DiscordMutationContext:
    """Build the narrow typed User Actor namespace mutation context for personal-feed tests."""
    return DiscordMutationContext(
        bridge_policy_service=_Policy(),
        user_actor_delivery_service=_DeliveryService(),
        user_actor_delivery_wake=SimpleNamespace(wake=lambda: None),
        fedify_gateway=SimpleNamespace(),
    )


def _database(tmp_path, name: str) -> Database:
    """Create one migrated SQLite database for a runtime scenario."""
    database = Database(f"sqlite:///{tmp_path / name}")
    database.migrate()
    return database


def _add_user(database: Database, discord_id: str, username: str) -> User:
    """Persist one registered local User Actor."""
    actor = f"https://bridge.example/actors/users/{username}"
    with database.session() as session:
        user = User(
            discord_user_id=discord_id,
            activitypub_username=username,
            actor_url=actor,
            inbox_url=f"{actor}/inbox",
            outbox_url=f"{actor}/outbox",
            followers_url=f"{actor}/followers",
            public_key_pem="public",
            private_key_pem="private",
        )
        session.add(user)
        session.flush()
        return user


def _add_root(database: Database, *, actor_url: str, message_id: int = 100) -> None:
    """Persist one personal-feed root publishing root with deterministic Discord identity."""
    with database.session() as session:
        session.add(
            PublishedActivityObject(
                **PlatformSurfaceRepository.discord_source_location(session, 10, 1),
                source_external_message_id=str(message_id),
                actor_username="alice",
                actor_url=actor_url,
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
                discord_message_id=message_id,
            )
        )


class _Gateway:
    """Render deterministic reply JSON at the external gateway boundary."""

    def __init__(self) -> None:
        self.requests: list[object] = []

    async def render_personal_feed(self, request: object) -> RenderPersonalFeedResult:
        """Return one canonical Create(Note) result for persistence assertions."""
        self.requests.append(request)
        activity = {
            "id": request.activity_id,
            "type": "Create",
            "actor": "https://bridge.example/actors/users/alice",
            "object": {
                "id": request.object_id,
                "type": "Note",
                "attributedTo": "https://bridge.example/actors/users/alice",
                "content": request.body_markdown,
                "inReplyTo": request.in_reply_to_object_id,
            },
        }
        return RenderPersonalFeedResult(
            request.activity_id,
            request.object_id,
            request.published_at,
            activity,
        )


def _conversation_runtime(
    database: Database, service: PersonalFeedService, gateway: object
):
    """Build the real personal-feed conversations runtime with only outer boundaries faked."""
    return PersonalFeedConversationRuntime(
        database=database,
        personal_feed_service=service,
        fedify_gateway=gateway,
        user_ban_service=SimpleNamespace(
            check_global_discord_user=lambda _id: SimpleNamespace(banned=False)
        ),
        bridge_policy_service=SimpleNamespace(
            is_discord_guild_allowed=lambda _id: True
        ),
        fedify_origin="https://bridge.example",
        delivery_worker=SimpleNamespace(wake=lambda: None),
    )


@pytest.mark.asyncio
async def test_bridgebot_thread_create_adopts_personal_root_before_community(
    tmp_path,
) -> None:
    """A manual root thread is durably adopted without entering community runtimes."""
    database = _database(tmp_path, "thread-create.sqlite3")
    user = _add_user(database, "10", "alice")
    _add_root(database, actor_url=user.actor_url)
    service = PersonalFeedService(database)
    service.bind_or_move("10", 1, 10)
    conversation = _conversation_runtime(database, service, _Gateway())
    community = SimpleNamespace(handle_discord_thread_create=AsyncMock())
    local = SimpleNamespace(handle_discord_thread_create=AsyncMock())
    router = DiscordEventRouter(
        database=database,
        community_runtime=community,
        local_community_runtime=local,
        bridge_policy_service=SimpleNamespace(
            is_discord_guild_allowed=lambda _id: True
        ),
        personal_feed_service=service,
        personal_feed_conversation_runtime=conversation,
    )
    bot = object.__new__(BridgeBot)
    bot.database = database
    bot.event_router = router
    bot._connection = SimpleNamespace(user=SimpleNamespace(id=999))
    # The production callback now submits one normalized event instead of
    # calling SDK-bound community handlers or looking up starter history.
    from src.platform_types import ChannelRef, ThreadCreated, ThreadRef

    class PersonalThreadAdapter:
        """Observe the SDK boundary and dispatch one trusted typed adoption."""

        async def ingest_thread_created(self, observed):
            """Adopt the exact physical Personal Feed root through its typed handler."""
            event = ThreadCreated(
                connector_key='discord-primary',
                thread=ThreadRef(ChannelRef('discord-primary', '10', 'text', '1'), str(observed.id)),
                name='', external_guild_id='1',
            )
            return await router.handle_platform_thread_create(event)

    bot.discord_adapter = PersonalThreadAdapter()
    bot._thread_locks = {}
    thread = SimpleNamespace(
        id=100, parent_id=10, owner_id=10, guild=SimpleNamespace(id=1)
    )

    await BridgeBot.on_thread_create(bot, thread)

    mapping = database.personal_feed_conversations.get_thread_by_thread_id(100)
    assert mapping is not None
    community.handle_discord_thread_create.assert_not_awaited()
    local.handle_discord_thread_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_bridgebot_registered_reply_persists_once_and_duplicate_is_safe(
    tmp_path, monkeypatch
) -> None:
    """A registered thread message becomes one durable Person-authored reply."""
    database = _database(tmp_path, "reply.sqlite3")
    root_user = _add_user(database, "10", "alice")
    _add_user(database, "20", "bob")
    _add_root(database, actor_url=root_user.actor_url)
    service = PersonalFeedService(database)
    service.bind_or_move("10", 1, 10)
    database.personal_feed_conversations.adopt_thread(
        root_object_id="https://bridge.example/o/root",
        channel_id=10,
        root_message_id=100,
        thread_id=100,
    )
    gateway = _Gateway()
    conversation = _conversation_runtime(database, service, gateway)
    router = DiscordEventRouter(
        database=database,
        community_runtime=SimpleNamespace(),
        local_community_runtime=SimpleNamespace(),
        bridge_policy_service=SimpleNamespace(
            is_discord_guild_allowed=lambda _id: True
        ),
        personal_feed_service=service,
        personal_feed_conversation_runtime=conversation,
    )

    class FakeThread:
        """Minimal Discord thread accepted by the bot adapter."""

        def __init__(self) -> None:
            self.id = 100
            self.parent_id = 10

    monkeypatch.setattr("src.discord_bot.discord.Thread", FakeThread)
    bot = object.__new__(BridgeBot)
    bot.database = database
    bot.event_router = router
    bot._connection = SimpleNamespace(user=SimpleNamespace(id=999))
    message = SimpleNamespace(
        id=200,
        guild=SimpleNamespace(id=1),
        channel=FakeThread(),
        author=SimpleNamespace(id=20, bot=False),
        content="reply from Bob",
        created_at=datetime.now(timezone.utc),
        reference=None,
    )

    await BridgeBot.on_message(bot, message)
    await BridgeBot.on_message(bot, message)

    assert len(gateway.requests) == 1
    with database.session() as session:
        rows = list(
            session.query(PublishedActivityObject).filter_by(kind="comment").all()
        )
        mappings = list(
            session.query(MessageMapping).filter_by(discord_message_id=200).all()
        )
        assert len(rows) == 1
        assert rows[0].actor_url.endswith("/actors/users/bob")
        assert rows[0].in_reply_to_object_id == "https://bridge.example/o/root"
        assert len(mappings) == 1


@pytest.mark.asyncio
async def test_unregistered_reply_responds_without_durable_side_effects(
    tmp_path,
) -> None:
    """An unregistered participant gets the existing response and no publication."""
    database = _database(tmp_path, "unregistered.sqlite3")
    root_user = _add_user(database, "10", "alice")
    _add_root(database, actor_url=root_user.actor_url)
    service = PersonalFeedService(database)
    service.bind_or_move("10", 1, 10)
    database.personal_feed_conversations.adopt_thread(
        root_object_id="https://bridge.example/o/root",
        channel_id=10,
        root_message_id=100,
        thread_id=100,
    )
    gateway = _Gateway()
    runtime = _conversation_runtime(database, service, gateway)
    reply = AsyncMock()
    message = SimpleNamespace(
        id=201,
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=100),
        author=SimpleNamespace(id=404),
        content="not registered",
        created_at=datetime.now(timezone.utc),
        reference=None,
        reply=reply,
    )

    result = await runtime.publish_local_reply(message=message)

    assert (result.status, result.reason) == ("rejected", "not_registered")
    reply.assert_awaited_once()
    assert gateway.requests == []
    with database.session() as session:
        assert (
            session.query(PublishedActivityObject).filter_by(kind="comment").count()
            == 0
        )
        assert (
            session.query(MessageMapping).filter_by(discord_message_id=201).count() == 0
        )


@pytest.mark.asyncio
async def test_raw_personal_feed_edit_and_delete_route_to_user_actor_mutations(
    tmp_path,
) -> None:
    """Raw Discord mutations use the personal mapping rather than community delivery."""
    database = _database(tmp_path, "raw.sqlite3")
    user = _add_user(database, "10", "alice")
    _add_root(database, actor_url=user.actor_url, message_id=100)
    with database.session() as session:
        session.add(
            MessageMapping(
                **PlatformSurfaceRepository.discord_source_location(session, 10, 1),
                source_platform="discord",
                source_id="100",
                activity_id="https://bridge.example/a/root",
                object_id="https://bridge.example/o/root",
                actor_url=user.actor_url,
                publication_context="personal_feed",
                community_actor_url=None,
                discord_guild_id=1,
                discord_channel_id=10,
                discord_message_id=100,
            )
        )
    edit = AsyncMock()
    delete = AsyncMock()
    router = SimpleNamespace(
        is_local_community_message=lambda _id: False,
        handle_message_edit=edit,
        handle_message_delete=delete,
    )
    bot = object.__new__(BridgeBot)
    bot.database = database
    bot.event_router = router
    bot._message_mutation_handler = None
    bot.get_channel = lambda _id: None
    bot.fetch_channel = AsyncMock()
    bot.set_message_mutation_handler(
        DiscordMessageMutationHandler(
            database=database,
            event_router=router,
            discord_fetcher=bot,
            mutation_context_provider=_mutation_context,
        )
    )
    edited_at = "2026-06-17T12:00:00Z"

    await BridgeBot.on_raw_message_edit(
        bot,
        SimpleNamespace(
            message_id=100,
            channel_id=10,
            guild_id=1,
            data={
                "content": "edited",
                "edited_timestamp": edited_at,
                "author": {"username": "alice"},
            },
        ),
    )
    await BridgeBot.on_raw_message_delete(
        bot, SimpleNamespace(message_id=100, channel_id=10, guild_id=1)
    )

    edit.assert_awaited_once()
    delete.assert_awaited_once()
    assert delete.await_args.kwargs["message_id"] == 100
    assert delete.await_args.kwargs["guild_id"] == 1
    assert "mutation_context" in delete.await_args.kwargs


@pytest.mark.asyncio
async def test_activitypub_personal_target_precedes_community_dispatch(
    tmp_path,
) -> None:
    """A normalized personal target is admitted without invoking community runtimes."""
    database = _database(tmp_path, "inbound.sqlite3")
    user = _add_user(database, "10", "alice")
    _add_root(database, actor_url=user.actor_url)
    service = PersonalFeedService(database)
    service.bind_or_move("10", 1, 10)
    conversation = _conversation_runtime(database, service, _Gateway())
    community = SimpleNamespace(handle_inbound_comment=AsyncMock())
    runtime = SimpleNamespace(
        database=database,
        personal_feed_conversation_runtime=conversation,
        community_runtime=community,
        local_community_runtime=SimpleNamespace(),
        bridge_policy_service=SimpleNamespace(
            federation_decision=lambda _subject: SimpleNamespace(allowed=True)
        ),
    )
    event = SimpleNamespace(
        event_type="comment.created",
        actor_id="https://remote.example/actors/users/bob",
        community_actor_id=None,
        delivery_id="https://remote.example/a/1",
        source_activity_id="https://remote.example/a/1",
        source_activity_json={"type": "Create"},
        target=SimpleNamespace(
            kind="personal_feed", root_object_id="https://bridge.example/o/root"
        ),
        object=SimpleNamespace(
            ap_id="https://remote.example/o/1",
            attributed_to_actor_id="https://remote.example/actors/users/bob",
            author_name="Bob",
            body_markdown="remote reply",
            parent_ap_id="https://bridge.example/o/root",
            published_at=datetime.now(timezone.utc),
        ),
    )

    result = await dispatch_activitypub_event(event, runtime)

    assert result.status == "processed"
    community.handle_inbound_comment.assert_not_awaited()
    assert (
        database.personal_feed_conversations.get_inbound_object(
            "https://remote.example/o/1"
        )
        is not None
    )


@pytest.mark.asyncio
async def test_deleted_root_blocks_later_discord_replies(tmp_path) -> None:
    """A terminal root deletion preserves mappings but prevents new replies."""
    database = _database(tmp_path, "deleted-root.sqlite3")
    root_user = _add_user(database, "10", "alice")
    _add_user(database, "20", "bob")
    _add_root(database, actor_url=root_user.actor_url)
    service = PersonalFeedService(database)
    service.bind_or_move("10", 1, 10)
    database.personal_feed_conversations.adopt_thread(
        root_object_id="https://bridge.example/o/root",
        channel_id=10,
        root_message_id=100,
        thread_id=100,
    )
    with database.session() as session:
        root = (
            session.query(PublishedActivityObject)
            .filter_by(object_id="https://bridge.example/o/root")
            .one()
        )
        root.deleted_at = datetime.now(timezone.utc)
    gateway = _Gateway()
    runtime = _conversation_runtime(database, service, gateway)
    message = SimpleNamespace(
        id=202,
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=100),
        author=SimpleNamespace(id=20),
        content="too late",
        created_at=datetime.now(timezone.utc),
        reference=None,
    )

    result = await runtime.publish_local_reply(message=message)

    assert (result.status, result.reason) == ("ignored", "root_deleted")
    assert gateway.requests == []
    with database.session() as session:
        assert (
            session.query(MessageMapping).filter_by(discord_message_id=202).count() == 0
        )
