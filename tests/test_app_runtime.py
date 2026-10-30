"""Production runtime composition regression coverage."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from src.app import build_runtime
from src.config import Settings
from src.discord_message_mutations import DiscordMessageMutationHandler


from bridge_test_support.runtime import runtime_settings as _settings


@pytest.mark.asyncio
async def test_build_runtime_wires_user_actor_follow_service_after_bot_and_closes_owners(
    tmp_path: Path,
) -> None:
    runtime = build_runtime(_settings(tmp_path))

    assert runtime.user_actor_follow_service.discord_notices is runtime.discord_adapter
    assert not hasattr(runtime.bot, "_runtime")
    assert runtime.bot._require_personal_feed_delivery_worker() is runtime.personal_feed_discord_delivery_worker
    assert isinstance(
        runtime.bot._require_message_mutation_handler(), DiscordMessageMutationHandler
    )
    assert (
        runtime.community_runtime.discord_fanout.mutation_tracker
        is runtime.bot._require_message_mutation_handler()
    )
    assert (
        runtime.user_profile_service.delivery_service
        is runtime.user_actor_delivery_service
    )
    assert (
        runtime.user_profile_service.delivery_worker
        is runtime.user_actor_delivery_worker
    )
    assert runtime.user_actor_delivery_service.discord_message_reader is runtime.discord_adapter
    publisher = runtime.bot.event_router.personal_feed_publisher
    assert publisher.user_ban_service is runtime.bot.user_ban_service
    assert publisher.bridge_policy_service is runtime.bridge_policy_service
    assert (
        runtime.bot.event_router.personal_feed_conversation_runtime
        is runtime.personal_feed_conversation_runtime
    )
    assert (
        runtime.personal_feed_conversation_runtime.delivery_worker
        is runtime.personal_feed_discord_delivery_worker
    )
    with pytest.raises(RuntimeError):
        runtime.bot.set_message_mutation_handler(runtime.bot._require_message_mutation_handler())
    with pytest.raises(RuntimeError):
        runtime.bot.bind_personal_feed_delivery_worker(runtime.personal_feed_discord_delivery_worker)
    with pytest.raises(RuntimeError):
        runtime.community_runtime.bind_discord_fanout(runtime.community_runtime.discord_fanout)
    with pytest.raises(RuntimeError):
        runtime.community_runtime.bind_discord_client(runtime.bot)
    with pytest.raises(RuntimeError):
        runtime.local_community_runtime.bind_discord_client(runtime.bot)
    with pytest.raises(RuntimeError):
        runtime.personal_feed_conversation_runtime.bind_delivery_wake(runtime.personal_feed_discord_delivery_worker)
    with pytest.raises(RuntimeError):
        runtime.user_actor_delivery_service.bind_discord_message_reader(runtime.bot)

    close_order: list[str] = []

    async def stop_discord_worker() -> None:
        close_order.append("discord-worker")

    async def stop_worker() -> None:
        close_order.append("worker")

    async def close_oauth() -> None:
        close_order.append("oauth")
        await original_oauth_close()

    async def close_gateway() -> None:
        close_order.append("gateway")
        await original_gateway_close()

    def close_database() -> None:
        close_order.append("database")
        original_database_close()

    def close_lease() -> None:
        close_order.append("lease")
        original_lease_close()

    original_oauth_close = runtime.discord_oauth_client.close
    original_gateway_close = runtime.fedify_gateway.close
    original_database_close = runtime.database.close
    original_lease_close = runtime.deployment_state_lease.close
    discord_worker_stop = AsyncMock(side_effect=stop_discord_worker)
    worker_stop = AsyncMock(side_effect=stop_worker)
    oauth_close = AsyncMock(side_effect=close_oauth)
    gateway_close = AsyncMock(side_effect=close_gateway)
    database_close = Mock(side_effect=close_database)
    lease_close = Mock(side_effect=close_lease)
    runtime.personal_feed_discord_delivery_worker.stop = discord_worker_stop
    runtime.user_actor_delivery_worker.stop = worker_stop
    runtime.discord_oauth_client.close = oauth_close
    runtime.fedify_gateway.close = gateway_close
    runtime.database.close = database_close
    runtime.deployment_state_lease.close = lease_close

    await runtime.close()

    discord_worker_stop.assert_awaited_once_with()
    worker_stop.assert_awaited_once_with()
    oauth_close.assert_awaited_once_with()
    gateway_close.assert_awaited_once_with()
    database_close.assert_called_once_with()
    lease_close.assert_called_once_with()
    assert close_order == [
        "discord-worker",
        "worker",
        "oauth",
        "gateway",
        "database",
        "lease",
    ]


@pytest.mark.asyncio
async def test_runtime_close_cancels_inflight_personal_feed_delivery_before_dependencies(
    tmp_path: Path,
) -> None:
    """Runtime shutdown interrupts Discord I/O before closing shared dependencies."""
    import asyncio
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from src.db import Database
    from src.models import InboundDiscordDelivery, PublishedActivityObject, User

    runtime = build_runtime(_settings(tmp_path))
    with runtime.database.session() as session:
        user = User(
            discord_user_id="10",
            activitypub_username="alice",
            actor_url="https://bridge.example/actors/users/alice",
            inbox_url="https://bridge.example/actors/users/alice/inbox",
            outbox_url="https://bridge.example/actors/users/alice/outbox",
            followers_url="https://bridge.example/actors/users/alice/followers",
            public_key_pem="public",
            private_key_pem="private",
        )
        session.add(user)
        session.flush()
        session.add(
            PublishedActivityObject(
                actor_username="alice",
                actor_url=user.actor_url,
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
        )
    runtime.bot.event_router.personal_feed_service.bind_or_move("10", 1, 10)
    admitted = runtime.database.personal_feed_conversations.admit_create(
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

    # The worker owns a typed PlatformAdapterPort, not a raw Discord bot.
    # Intercept its physical channel lookup without mutating the live SDK.
    runtime.personal_feed_discord_delivery_worker.adapter.fetch_channel = blocked_fetch
    close_order: list[str] = []
    original_database_close = runtime.database.close
    original_lease_close = runtime.deployment_state_lease.close
    runtime.user_actor_delivery_worker.stop = AsyncMock(
        side_effect=lambda: close_order.append("user-worker")
    )
    runtime.discord_oauth_client.close = AsyncMock(
        side_effect=lambda: close_order.append("oauth")
    )
    runtime.fedify_gateway.close = AsyncMock(
        side_effect=lambda: close_order.append("gateway")
    )
    runtime.database.close = Mock(
        side_effect=lambda: (close_order.append("database"), original_database_close())
    )
    runtime.deployment_state_lease.close = Mock(
        side_effect=lambda: (close_order.append("lease"), original_lease_close())
    )
    runtime.personal_feed_discord_delivery_worker.start()
    await asyncio.wait_for(entered.wait(), timeout=1)

    await asyncio.wait_for(runtime.close(), timeout=1)

    assert close_order == ["user-worker", "oauth", "gateway", "database", "lease"]
    with Database(f"sqlite:///{tmp_path / 'bridge.db'}").session() as session:
        delivery = session.get(InboundDiscordDelivery, admitted.delivery_id)
        assert delivery.state == "ready"
        assert delivery.attempt_count == 0



@pytest.mark.parametrize("phase", ["migration", "oauth", "validation"])
def test_partial_startup_closes_real_resources_and_releases_both_leases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """Every composition milestone unwinds SQLite before both protective locks."""
    from src import app
    from src.db import Database
    from src.profile_media import FileLock
    from src.runtime_ownership import acquire_runtime_lifetime_lease

    settings = _settings(tmp_path)
    steps = []
    migrate, close, unlock = Database.migrate, Database.close, FileLock.close

    def migrate_database(database, *args, **kwargs):
        """Fail only after opening and migrating genuine SQLite connections."""
        migrate(database, *args, **kwargs)
        if phase == "migration":
            raise RuntimeError("migration failed")

    def close_database(database):
        """Observe real disposal without replacing it with mock success."""
        close(database)
        steps.append("database")

    def close_lease(lease):
        """Record reverse-order release of real acquired lock descriptors."""
        unlock(lease)
        steps.append(lease.path.name)

    def fail(*args, **kwargs):
        """Inject an outer construction or final composition boundary error."""
        raise RuntimeError(f"{phase} failed")

    monkeypatch.setattr(Database, "migrate", migrate_database)
    monkeypatch.setattr(Database, "close", close_database)
    monkeypatch.setattr(FileLock, "close", close_lease)
    if phase == "oauth":
        monkeypatch.setattr(app, "DiscordOAuthClient", fail)
    if phase == "validation":
        monkeypatch.setattr(app, "_validate_runtime_graph", fail)
    with pytest.raises(RuntimeError, match=f"{phase} failed"):
        build_runtime(settings)
    assert steps[-3:] == ["database", "deployment-state.lock", "bridge.db.runtime.lock"]
    with acquire_runtime_lifetime_lease(settings.database_url):
        with FileLock(Path(settings.deployment_state_lock_path)).acquire(
            exclusive=True, timeout_seconds=0
        ):
            pass
