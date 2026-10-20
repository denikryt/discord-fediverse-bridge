"""Production runtime composition regression coverage."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from src.app import build_runtime
from src.config import Settings
from src.discord_message_mutations import DiscordMessageMutationHandler


def _settings(state_root: Path) -> Settings:
    """Build isolated settings while preserving production validation defaults."""
    validated = Settings.model_validate(
        {
            "DISCORD_TOKEN": "test-discord-token",
            "DATABASE_URL": "sqlite:////data/bridge.db",
            "PUBLIC_BASE_URL": "https://bridge.example",
            "BRIDGE_GATEWAY_URL": "http://127.0.0.1:3000",
            "FEDIFY_SHARED_SECRET": "test-shared-secret",
            "BRIDGE_SUPER_ADMIN_USER_IDS": "1234567890",
            "PROFILE_MEDIA_ROOT": "/data/profile-media",
            "DEPLOYMENT_STATE_LOCK_PATH": "/data/deployment-state.lock",
            "PROFILE_MEDIA_LOCK_PATH": "/data/profile-media.lock",
        }
    )
    values = validated.model_dump()
    values.update(
        database_url=f"sqlite:///{state_root / 'bridge.db'}",
        profile_media_root=str(state_root / "profile-media"),
        deployment_state_lock_path=str(state_root / "deployment-state.lock"),
        profile_media_lock_path=str(state_root / "profile-media.lock"),
    )
    return Settings.model_construct(**values)


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

    runtime.personal_feed_discord_delivery_worker.bot = SimpleNamespace(
        get_channel=lambda _id: None,
        fetch_channel=blocked_fetch,
    )
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


def test_partial_startup_failure_after_database_closes_database_and_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration-time startup failure closes only owners that already exist."""
    from src import app

    close_order: list[str] = []

    class FakeLease:
        """Track deployment lease closure for a failed startup."""

        def close(self) -> None:
            """Record release of the shared deployment lease."""
            close_order.append("lease")

    class FakeDatabase:
        """Fail immediately after the database owner is constructed."""

        def __init__(self, _url: str) -> None:
            close_order.append("database-open")

        def create_all(self) -> None:
            """Preserve the schema creation milestone before migration."""

        def migrate(self, *_args: object, **_kwargs: object) -> None:
            """Raise after DB construction so cleanup must dispose the engine."""
            raise RuntimeError("migration failed")

        def close(self) -> None:
            """Record disposal of the partially opened database owner."""
            close_order.append("database")

    monkeypatch.setattr(app, "Database", FakeDatabase)

    with pytest.raises(RuntimeError, match="migration failed"):
        app._build_live_runtime(_settings(tmp_path), FakeLease())

    assert close_order == ["database-open", "database", "lease"]


def test_partial_startup_failure_after_gateway_closes_gateway_database_and_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after gateway construction closes gateway before database and lease."""
    from src import app

    close_order: list[str] = []

    class FakeLease:
        """Track deployment lease closure for gateway failure."""

        def close(self) -> None:
            """Record lease release."""
            close_order.append("lease")

    class FakeDatabase:
        """Provide only the database surface needed before gateway construction."""

        def __init__(self, _url: str) -> None:
            close_order.append("database-open")
            self.bridge_policy_entries = object()

        def create_all(self) -> None:
            """No-op schema setup for composition failure testing."""

        def migrate(self, *_args: object, **_kwargs: object) -> None:
            """No-op migration setup for composition failure testing."""

        def close(self) -> None:
            """Record database disposal."""
            close_order.append("database")

    class FakeGateway:
        """Represent a constructed gateway HTTP client."""

        def __init__(self, _settings: object) -> None:
            close_order.append("gateway-open")

        async def close(self) -> None:
            """Record asynchronous gateway cleanup."""
            close_order.append("gateway")

    class FailingOAuth:
        """Fail immediately after the gateway has become a live owner."""

        def __init__(self, _settings: object) -> None:
            raise RuntimeError("oauth failed")

    monkeypatch.setattr(app, "Database", FakeDatabase)
    monkeypatch.setattr(app, "BridgeActorKeyBootstrap", lambda **_kwargs: Mock(ensure=Mock()))
    monkeypatch.setattr(app, "FedifyGatewayClient", FakeGateway)
    monkeypatch.setattr(app, "DiscordOAuthClient", FailingOAuth)

    with pytest.raises(RuntimeError, match="oauth failed"):
        app._build_live_runtime(_settings(tmp_path), FakeLease())

    assert close_order == ["database-open", "gateway-open", "gateway", "database", "lease"]


@pytest.mark.asyncio
async def test_partial_startup_validation_failure_closes_complete_runtime_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A final graph-validation failure uses Runtime.close() before returning."""
    from src import app
    from src.db import Database as RealDatabase
    from src.discord_oauth_client import DiscordOAuthClient as RealOAuth
    from src.fedify_gateway_client import FedifyGatewayClient as RealGateway

    close_order: list[str] = []

    class FakeLease:
        """Track deployment lease closure after final graph validation fails."""

        def close(self) -> None:
            """Record release of the deployment lease."""
            close_order.append("lease")

    class TrackingDatabase(RealDatabase):
        """Real database with observable close semantics."""

        def close(self) -> None:
            """Record database disposal without hiding real cleanup."""
            close_order.append("database")
            super().close()

    class TrackingGateway(RealGateway):
        """Real gateway client with observable async close semantics."""

        async def close(self) -> None:
            """Record gateway HTTP client cleanup."""
            close_order.append("gateway")
            await super().close()

    class TrackingOAuth(RealOAuth):
        """Real OAuth client with observable async close semantics."""

        async def close(self) -> None:
            """Record OAuth HTTP client cleanup."""
            close_order.append("oauth")
            await super().close()

    monkeypatch.setattr(app, "Database", TrackingDatabase)
    monkeypatch.setattr(app, "FedifyGatewayClient", TrackingGateway)
    monkeypatch.setattr(app, "DiscordOAuthClient", TrackingOAuth)

    def fail_validation(_runtime: object) -> None:
        """Fail after all production bind methods have completed."""
        raise RuntimeError("validation failed")

    monkeypatch.setattr(app, "_validate_runtime_graph", fail_validation)

    with pytest.raises(RuntimeError, match="validation failed"):
        app._build_live_runtime(_settings(tmp_path), FakeLease())

    assert close_order == ["oauth", "gateway", "database", "lease"]
