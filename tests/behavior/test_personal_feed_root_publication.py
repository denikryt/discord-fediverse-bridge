"""personal-feed root publishing runtime scenarios for personal-feed root publication."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.content_sync.persistence import PublicationConflictError
from src.db import Database
from src.fedify_gateway_client import RenderPersonalFeedResult
from src.models import (
    MessageMapping,
    PublishedActivityObject,
    User,
    UserActorOutboundSource,
)
from src.personal_feed_publisher import PersonalFeedPublisher
from src.personal_feeds import PersonalFeedService


def _user() -> User:
    """Build one deterministic registered User Actor."""
    return User(
        discord_user_id="10",
        activitypub_username="alice",
        actor_url="https://bridge.test/actors/users/alice",
        inbox_url="https://bridge.test/actors/users/alice/inbox",
        outbox_url="https://bridge.test/actors/users/alice/outbox",
        followers_url="https://bridge.test/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )


class Gateway:
    """Render canonical personal-feed JSON without delivery side effects."""

    def __init__(self) -> None:
        self.requests = []

    async def render_personal_feed(self, request):
        """Return JSON using the exact deterministic request identity."""
        self.requests.append(request)
        actor = "https://bridge.test/actors/users/alice"
        followers = f"{actor}/followers"
        activity = {
            "type": "Create",
            "id": request.activity_id,
            "actor": actor,
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "cc": [actor, followers],
            "object": {
                "type": "Note",
                "id": request.object_id,
                "url": request.object_id,
                "attributedTo": actor,
                "to": ["https://www.w3.org/ns/activitystreams#Public"],
                "cc": [followers],
                "source": {
                    "mediaType": "text/markdown",
                    "content": request.body_markdown,
                },
                "content": f"<p>{request.body_markdown}</p>",
                "published": request.published_at,
            },
        }
        return RenderPersonalFeedResult(
            request.activity_id, request.object_id, request.published_at, activity
        )


@pytest.mark.asyncio
async def test_owner_root_persists_one_canonical_note_and_duplicate_is_side_effect_free(
    tmp_path,
) -> None:
    """One owner root creates one durable mapping/object/source and replays reuse it."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.sqlite3'}")
    database.migrate()
    with database.session() as session:
        session.add(_user())
    bindings = PersonalFeedService(database)
    bound = bindings.bind_or_move("10", 100, 200).binding
    gateway = Gateway()
    worker = SimpleNamespace(
        wake=lambda: setattr(worker, "wakes", worker.wakes + 1), wakes=0
    )
    publisher = PersonalFeedPublisher(
        database=database,
        personal_feed_service=bindings,
        fedify_gateway=gateway,
        user_ban_service=SimpleNamespace(
            check_global_discord_user=lambda _id: SimpleNamespace(banned=False)
        ),
        bridge_policy_service=SimpleNamespace(
            is_discord_guild_allowed=lambda _id: True
        ),
        fedify_origin="https://bridge.test",
        delivery_worker=worker,
    )
    message = SimpleNamespace(
        id=300,
        guild=SimpleNamespace(id=100),
        channel=SimpleNamespace(id=200),
        author=SimpleNamespace(id=10),
        content="complete root body",
        created_at=datetime(2026, 6, 17, 12, 34, 56, 789000, tzinfo=timezone.utc),
    )

    first = await publisher.publish_root(message=message, binding=bound)
    second = await publisher.publish_root(message=message, binding=bound)

    assert first.status == "published"
    assert second.status == "already_published"
    assert len(gateway.requests) == 1
    assert worker.wakes == 1
    with database.session() as session:
        mapping = session.scalar(select(MessageMapping))
        published = session.scalar(select(PublishedActivityObject))
        source = session.scalar(select(UserActorOutboundSource))
        assert mapping.publication_context == "personal_feed"
        assert mapping.community_actor_url is None
        assert (
            mapping.discord_guild_id,
            mapping.discord_channel_id,
            mapping.discord_message_id,
        ) == (100, 200, 300)
        assert published.publication_context == "personal_feed"
        assert published.published_at == datetime(2026, 6, 17, 12, 34, 56, 789000)
        assert (
            json.loads(source.activity_json)["object"]["published"]
            == "2026-06-17T12:34:56.789Z"
        )


@pytest.mark.asyncio
async def test_non_owner_and_moved_binding_are_ignored_without_gateway_or_rows(
    tmp_path,
) -> None:
    """Authorship and current binding are revalidated immediately before rendering."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.sqlite3'}")
    database.migrate()
    with database.session() as session:
        session.add(_user())
    bindings = PersonalFeedService(database)
    old = bindings.bind_or_move("10", 100, 200).binding
    gateway = Gateway()
    publisher = PersonalFeedPublisher(
        database=database,
        personal_feed_service=bindings,
        fedify_gateway=gateway,
        user_ban_service=SimpleNamespace(
            check_global_discord_user=lambda _id: SimpleNamespace(banned=False)
        ),
        bridge_policy_service=SimpleNamespace(
            is_discord_guild_allowed=lambda _id: True
        ),
        fedify_origin="https://bridge.test",
    )
    base = dict(
        id=300,
        guild=SimpleNamespace(id=100),
        channel=SimpleNamespace(id=200),
        content="root",
        created_at=datetime.now(timezone.utc),
    )
    assert (
        await publisher.publish_root(
            message=SimpleNamespace(**base, author=SimpleNamespace(id=20)), binding=old
        )
    ).reason == "not_owner"
    bindings.bind_or_move("10", 101, 201, expected_binding_id=old.id)
    assert (
        await publisher.publish_root(
            message=SimpleNamespace(**base, author=SimpleNamespace(id=10)), binding=old
        )
    ).reason == "binding_changed"
    assert gateway.requests == []
    with database.session() as session:
        assert session.scalar(select(MessageMapping)) is None


def test_database_rejects_incomplete_personal_feed_source_identity(tmp_path) -> None:
    """Conditional checks protect every writer, migration, and repair path."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.sqlite3'}")
    database.migrate()
    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.add(
                MessageMapping(
                    source_platform="discord",
                    source_id="300",
                    activity_id="https://bridge.test/a",
                    object_id="https://bridge.test/o",
                    actor_url="https://bridge.test/actors/users/alice",
                    publication_context="personal_feed",
                    community_actor_url=None,
                    discord_guild_id=100,
                    discord_channel_id=None,
                    discord_message_id=300,
                )
            )
    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.add(
                PublishedActivityObject(
                    actor_username="alice",
                    actor_url="https://bridge.test/actors/users/alice",
                    publication_context="personal_feed",
                    community_actor_url=None,
                    activity_id="https://bridge.test/a2",
                    object_id="https://bridge.test/o2",
                    kind="post",
                    title=None,
                    body_markdown="root",
                    in_reply_to_object_id=None,
                    discord_guild_id=100,
                    discord_channel_id=200,
                    discord_message_id=None,
                )
            )


@pytest.mark.asyncio
async def test_policy_revoked_after_routing_is_rejected_before_gateway(
    tmp_path,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'bridge.sqlite3'}")
    database.migrate()
    with database.session() as session:
        session.add(_user())
    bindings = PersonalFeedService(database)
    bound = bindings.bind_or_move("10", 100, 200).binding
    gateway = Gateway()
    publisher = PersonalFeedPublisher(
        database=database,
        personal_feed_service=bindings,
        fedify_gateway=gateway,
        user_ban_service=SimpleNamespace(
            check_global_discord_user=lambda _id: SimpleNamespace(banned=False)
        ),
        bridge_policy_service=SimpleNamespace(
            is_discord_guild_allowed=lambda _id: False
        ),
        fedify_origin="https://bridge.test",
    )
    message = SimpleNamespace(
        id=300,
        guild=SimpleNamespace(id=100),
        channel=SimpleNamespace(id=200),
        author=SimpleNamespace(id=10),
        content="root",
        created_at=datetime.now(timezone.utc),
    )
    result = await publisher.publish_root(message=message, binding=bound)
    assert (result.status, result.reason) == ("rejected", "guild_disallowed")
    assert gateway.requests == []
    with database.session() as session:
        assert session.scalar(select(MessageMapping)) is None


@pytest.mark.asyncio
async def test_preflight_duplicate_fails_closed_when_canonical_object_is_missing(
    tmp_path,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'bridge.sqlite3'}")
    database.migrate()
    with database.session() as session:
        user = _user()
        session.add(user)
        session.flush()
    bindings = PersonalFeedService(database)
    bound = bindings.bind_or_move("10", 100, 200).binding
    message = SimpleNamespace(
        id=300,
        guild=SimpleNamespace(id=100),
        channel=SimpleNamespace(id=200),
        author=SimpleNamespace(id=10),
        content="root",
        created_at=datetime(2026, 6, 17, tzinfo=timezone.utc),
    )
    publisher = PersonalFeedPublisher(
        database=database,
        personal_feed_service=bindings,
        fedify_gateway=Gateway(),
        user_ban_service=SimpleNamespace(
            check_global_discord_user=lambda _id: SimpleNamespace(banned=False)
        ),
        bridge_policy_service=SimpleNamespace(
            is_discord_guild_allowed=lambda _id: True
        ),
        fedify_origin="https://bridge.test",
    )
    operation_id = (
        __import__("hashlib").sha256(b"personal-feed:100:200:300:1").hexdigest()
    )
    with database.session() as session:
        session.add(
            MessageMapping(
                **database.platform_surfaces.discord_source_location(session, 200),
                source_platform="discord",
                source_id="300",
                activity_id=f"https://bridge.test/actors/users/alice/activities/create/posts/{operation_id}",
                object_id=f"https://bridge.test/actors/users/alice/objects/posts/{operation_id}",
                actor_url="https://bridge.test/actors/users/alice",
                publication_context="personal_feed",
                community_actor_url=None,
                discord_guild_id=100,
                discord_channel_id=200,
                discord_message_id=300,
            )
        )
    with pytest.raises(Exception, match="without canonical published object"):
        await publisher.publish_root(message=message, binding=bound)


def _publisher_fixture(tmp_path, *, gateway=None, banned=False):
    database = Database(f"sqlite:///{tmp_path / 'bridge.sqlite3'}")
    database.migrate()
    with database.session() as session:
        session.add(_user())
    bindings = PersonalFeedService(database)
    bound = bindings.bind_or_move("10", 100, 200).binding
    gateway = gateway or Gateway()
    publisher = PersonalFeedPublisher(
        database=database,
        personal_feed_service=bindings,
        fedify_gateway=gateway,
        user_ban_service=SimpleNamespace(
            check_global_discord_user=lambda _id: SimpleNamespace(banned=banned)
        ),
        bridge_policy_service=SimpleNamespace(
            is_discord_guild_allowed=lambda _id: True
        ),
        fedify_origin="https://bridge.test",
    )
    message = SimpleNamespace(
        id=300,
        guild=SimpleNamespace(id=100),
        channel=SimpleNamespace(id=200),
        author=SimpleNamespace(id=10, bot=False),
        content="x" * 700,
        created_at=datetime(2026, 6, 17, 12, 34, 56, 789000, tzinfo=timezone.utc),
    )
    return database, bindings, bound, gateway, publisher, message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "published_context",
        "published_channel",
        "published_message",
        "published_kind",
        "published_reply",
        "source_kind",
        "source_version",
    ],
)
async def test_duplicate_replay_rejects_conflicting_canonical_graph(
    tmp_path, corruption
) -> None:
    """Every durable copy must match before a replay is classified as duplicate."""
    database, _, bound, gateway, publisher, message = _publisher_fixture(tmp_path)
    assert (
        await publisher.publish_root(message=message, binding=bound)
    ).status == "published"

    with database.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        source = session.scalar(select(UserActorOutboundSource))
        if corruption == "published_context":
            published.publication_context = "community"
            published.community_actor_url = (
                "https://bridge.test/actors/communities/wrong"
            )
        elif corruption == "published_channel":
            published.discord_channel_id = 999
        elif corruption == "published_message":
            published.discord_message_id = 999
        elif corruption == "published_kind":
            published.kind = "comment"
        elif corruption == "published_reply":
            published.in_reply_to_object_id = "https://remote.test/parent"
        elif corruption == "source_kind":
            source.source_kind = "content_update"
        elif corruption == "source_version":
            source.logical_version = 2

    with pytest.raises(PublicationConflictError):
        await publisher.publish_root(message=message, binding=bound)
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_global_ban_rejects_before_gateway_and_persistence(tmp_path) -> None:
    database, _, bound, gateway, publisher, message = _publisher_fixture(
        tmp_path, banned=True
    )
    result = await publisher.publish_root(message=message, binding=bound)
    assert (result.status, result.reason) == ("rejected", "user_banned")
    assert gateway.requests == []
    with database.session() as session:
        assert session.scalar(select(MessageMapping)) is None


@pytest.mark.asyncio
async def test_gateway_failure_leaves_no_rows_and_replay_is_deterministic(
    tmp_path,
) -> None:
    class FailingOnceGateway(Gateway):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        async def render_personal_feed(self, request):
            self.requests.append(request)
            if not self.failed:
                self.failed = True
                raise RuntimeError("render unavailable")
            self.requests.pop()
            return await super().render_personal_feed(request)

    gateway = FailingOnceGateway()
    database, _, bound, _, publisher, message = _publisher_fixture(
        tmp_path, gateway=gateway
    )
    with pytest.raises(RuntimeError, match="render unavailable"):
        await publisher.publish_root(message=message, binding=bound)
    with database.session() as session:
        assert session.scalar(select(MessageMapping)) is None

    result = await publisher.publish_root(message=message, binding=bound)
    assert result.status == "published"
    assert gateway.requests[0] == gateway.requests[1]
    with database.session() as session:
        published = session.scalar(select(PublishedActivityObject))
        assert published.body_markdown == "x" * 700


@pytest.mark.asyncio
async def test_commit_failure_after_render_replays_same_identity(
    tmp_path, monkeypatch
) -> None:
    import src.personal_feed_publisher as publisher_module

    database, _, bound, gateway, publisher, message = _publisher_fixture(tmp_path)
    real_persist = publisher_module.persist_publish_artifacts
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("commit failed")
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(publisher_module, "persist_publish_artifacts", fail_once)
    with pytest.raises(RuntimeError, match="commit failed"):
        await publisher.publish_root(message=message, binding=bound)
    with database.session() as session:
        assert session.scalar(select(MessageMapping)) is None

    result = await publisher.publish_root(message=message, binding=bound)
    assert result.status == "published"
    assert gateway.requests[0] == gateway.requests[1]


@pytest.mark.asyncio
async def test_concurrent_duplicate_publishers_commit_one_publication(tmp_path) -> None:
    import asyncio

    class BarrierGateway(Gateway):
        def __init__(self) -> None:
            super().__init__()
            self.arrived = 0
            self.release = asyncio.Event()

        async def render_personal_feed(self, request):
            self.arrived += 1
            if self.arrived == 2:
                self.release.set()
            await self.release.wait()
            return await super().render_personal_feed(request)

    gateway = BarrierGateway()
    database, bindings, bound, _, publisher_one, message = _publisher_fixture(
        tmp_path, gateway=gateway
    )
    publisher_two = PersonalFeedPublisher(
        database=database,
        personal_feed_service=bindings,
        fedify_gateway=gateway,
        user_ban_service=publisher_one.user_ban_service,
        bridge_policy_service=publisher_one.bridge_policy_service,
        fedify_origin="https://bridge.test",
    )
    results = await asyncio.gather(
        publisher_one.publish_root(message=message, binding=bound),
        publisher_two.publish_root(message=message, binding=bound),
    )
    assert sorted(result.status for result in results) == [
        "already_published",
        "published",
    ]
    with database.session() as session:
        assert len(list(session.scalars(select(MessageMapping)))) == 1
        assert len(list(session.scalars(select(PublishedActivityObject)))) == 1
        assert len(list(session.scalars(select(UserActorOutboundSource)))) == 1


@pytest.mark.asyncio
async def test_restart_replay_reuses_durable_publication_without_render(
    tmp_path,
) -> None:
    database, _, bound, _, publisher, message = _publisher_fixture(tmp_path)
    assert (
        await publisher.publish_root(message=message, binding=bound)
    ).status == "published"

    reopened = Database(f"sqlite:///{tmp_path / 'bridge.sqlite3'}")
    reopened.migrate()
    bindings = PersonalFeedService(reopened)
    gateway = Gateway()
    restarted = PersonalFeedPublisher(
        database=reopened,
        personal_feed_service=bindings,
        fedify_gateway=gateway,
        user_ban_service=publisher.user_ban_service,
        bridge_policy_service=publisher.bridge_policy_service,
        fedify_origin="https://bridge.test",
    )
    current = bindings.resolve_root_binding(100, 200)
    result = await restarted.publish_root(message=message, binding=current)
    assert result.status == "already_published"
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_bridge_bot_on_message_routes_announcement_personal_feed_root() -> None:
    """Announcement-channel bindings follow the real bot root-message path."""
    import discord

    from src.discord_bot import BridgeBot

    binding = SimpleNamespace(id=1)
    calls = []
    channel = object.__new__(discord.TextChannel)
    # Discord API GUILD_ANNOUNCEMENT; discord.py exposes it as news.
    channel._type = discord.ChannelType.news.value
    channel.id = 200
    message = SimpleNamespace(
        id=300,
        guild=SimpleNamespace(id=100),
        channel=channel,
        author=SimpleNamespace(id=10, bot=False),
    )
    router = SimpleNamespace(
        resolve_personal_feed_root=lambda guild_id, channel_id: binding,
        handle_personal_feed_root=lambda **kwargs: _record_async(calls, kwargs),
    )
    bot = SimpleNamespace(user=None, event_router=router)

    await BridgeBot.on_message(bot, message)

    assert calls == [{"message": message, "binding": binding}]


@pytest.mark.asyncio
async def test_bridge_bot_on_message_ignores_unbound_announcement_root() -> None:
    """Announcement classification still requires a durable personal-feed binding."""
    import discord

    from src.discord_bot import BridgeBot

    calls = []
    channel = object.__new__(discord.TextChannel)
    # Discord API GUILD_ANNOUNCEMENT; discord.py exposes it as news.
    channel._type = discord.ChannelType.news.value
    channel.id = 200
    message = SimpleNamespace(
        id=300,
        guild=SimpleNamespace(id=100),
        channel=channel,
        author=SimpleNamespace(id=10, bot=False),
    )
    router = SimpleNamespace(
        resolve_personal_feed_root=lambda guild_id, channel_id: None,
        handle_personal_feed_root=lambda **kwargs: _record_async(calls, kwargs),
    )
    bot = SimpleNamespace(user=None, event_router=router)

    await BridgeBot.on_message(bot, message)

    assert calls == []


async def _record_async(calls, value):
    calls.append(value)
