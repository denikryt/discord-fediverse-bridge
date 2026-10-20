"""Process startup and runtime wiring for the Discord bridge and FastAPI server."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from pathlib import Path

import uvicorn

from .actor_key_service import BridgeActorKeyBootstrap
from .bridge_policy import BridgePolicyService
from .community_sync.discord_fanout import DiscordFanout
from .community_sync.runtime import CommunityRuntime
from .config import Settings
from .db import Database
from .discord_bot import BridgeBot
from .discord_adapter import DiscordAdapter
from .platform_types import ConnectorRef
from .platform_event_ingress import PlatformEventIngress
from .platform_adapters import PlatformAdapterRegistry
from .discord_event_router import DiscordEventRouter
from .discord_message_mutations import DiscordMessageMutationHandler
from .discord_oauth_client import DiscordOAuthClient
from .content_publish_service import ContentPublishService
from .fedify_gateway_client import FedifyGatewayClient
from .http_api import create_http_app
from .local_communities.runtime import LocalCommunityRuntime
from .logging_setup import configure_logging
from .registration_service import RegistrationService
from .project_version import APP_VERSION
from .runtime import Runtime
from .runtime_ports import DiscordMutationContext
from .profile_media import FileLock, ProfileMediaStore
from .user_profiles import UserProfileService
from .user_actor_followers import UserActorFollowService
from .user_actor_delivery import UserActorDeliveryService
from .user_actor_delivery_worker import UserActorDeliveryWorker
from .personal_feeds import PersonalFeedService
from .personal_feed_publisher import PersonalFeedPublisher
from .personal_feed_conversations import PersonalFeedConversationRuntime
from .personal_feed_discord_delivery_worker import PersonalFeedDiscordDeliveryWorker
from .user_bans import UserBanService
from .db.backup import recover_incomplete_restore

logger = logging.getLogger(__name__)


async def _run_bot_lifetime(runtime: Runtime, discord_token: str) -> None:
    """Keep both delivery workers and Gateway tasks inside the SDK lifetime.

    Discord's callback tasks are not implicitly joined by Client.close(). The
    order here is owned by the existing run_bot process task, not a new manager.
    Each cleanup step runs even when the prior worker stop raises.
    """
    async with runtime.bot:
        try:
            await runtime.bot.start(discord_token)
        finally:
            # The gate must close before the first shutdown await: a delayed
            # on_ready must not restart a worker while other tasks are draining.
            runtime.discord_adapter.close_ingress_admission()
            try:
                await runtime.discord_adapter.drain_ingress()
            finally:
                try:
                    await runtime.personal_feed_discord_delivery_worker.stop()
                finally:
                    try:
                        await runtime.user_actor_delivery_worker.stop()
                    finally:
                        # SDK transport must outlive both in-flight workers;
                        # only Runtime.close() owns Fedify and the database.
                        await runtime.discord_adapter.quiesce()


async def main() -> None:
    # Build the full runtime once and then run both long-lived entry points
    # against the same shared state.
    settings = Settings()
    configure_logging(settings.log_level)
    logger.info("Starting Discord/Fediverse bridge version=%s", APP_VERSION)
    runtime = build_runtime(settings)
    bot = runtime.bot
    http_app = create_http_app(runtime)
    http_server = uvicorn.Server(
        uvicorn.Config(
            http_app,
            host=settings.internal_http_host,
            port=settings.internal_http_port,
            log_level=settings.log_level.lower(),
        )
    )

    async def run_bot() -> None:
        """Run the existing Gateway task under its ordered shutdown contract."""
        await _run_bot_lifetime(runtime, settings.discord_token)

    async def run_http_server() -> None:
        await http_server.serve()

    try:
        # Recovery starts before either long-lived server task, but inside the
        # same close boundary so partial startup cannot leak its DB/gateway use.
        runtime.user_actor_delivery_worker.start()
        async with asyncio.TaskGroup() as task_group:
            # Both tasks are mandatory for the bridge to function, so they share one lifecycle.
            task_group.create_task(run_bot(), name="discord-bot")
            task_group.create_task(run_http_server(), name="internal-http-server")
    finally:
        await runtime.close()


def build_runtime(settings: Settings) -> Runtime:
    """Recover durable state, acquire a lifetime lease, and build the runtime."""
    deployment_lock_path = Path(settings.deployment_state_lock_path)
    with FileLock(deployment_lock_path).acquire(
        exclusive=True, timeout_seconds=settings.profile_media_lock_timeout_seconds
    ):
        recover_incomplete_restore(
            Path(settings.database_url.removeprefix("sqlite:///")),
            Path(settings.profile_media_root),
        )
    deployment_lease = FileLock(deployment_lock_path).acquire(
        exclusive=False, timeout_seconds=settings.profile_media_lock_timeout_seconds
    )
    return _build_live_runtime(settings, deployment_lease)


def _build_live_runtime(settings: Settings, deployment_lease: FileLock) -> Runtime:
    """Open live runtime owners and clean them up on partial startup failure."""
    closeables: list[object] = [deployment_lease]
    runtime: Runtime | None = None
    try:
        database = Database(settings.database_url)
        closeables.append(database)
        media_store = ProfileMediaStore(
            Path(settings.profile_media_root),
            max_upload_bytes=settings.profile_media_max_upload_bytes,
            max_pixels=settings.profile_media_max_pixels,
        )
        media_store.initialize()
        from .db.legacy_identity_cutover import IdentityCutoverConfig

        database.migrate(
            identity=IdentityCutoverConfig(
                settings.normalized_fedify_origin, settings.fedify_actor_identifier
            )
        )
        BridgeActorKeyBootstrap(database=database, settings=settings).ensure()

        bridge_policy_service = BridgePolicyService(
            settings=settings, repository=database.bridge_policy_entries
        )
        fedify_gateway = FedifyGatewayClient(settings)
        closeables.append(fedify_gateway)
        discord_oauth_client = DiscordOAuthClient(settings)
        closeables.append(discord_oauth_client)
        user_actor_delivery_service = UserActorDeliveryService(
            database, fedify_gateway, bridge_policy_service
        )
        user_actor_delivery_worker = UserActorDeliveryWorker(
            user_actor_delivery_service
        )
        content_publish_service = ContentPublishService(
            database=database,
            fedify_gateway=fedify_gateway,
            bridge_prefix=settings.bridge_display_prefix,
            settings=settings,
            bridge_policy_service=bridge_policy_service,
            delivery_worker=user_actor_delivery_worker,
        )
        user_profile_service = UserProfileService(
            database,
            media_store,
            settings.normalized_fedify_origin,
            delivery_service=user_actor_delivery_service,
            delivery_worker=user_actor_delivery_worker,
        )
        registration_service = RegistrationService(
            database=database,
            base_url=settings.normalized_fedify_origin,
        )
        local_community_runtime = LocalCommunityRuntime(
            database=database,
            fedify_gateway=fedify_gateway,
            content_publish_service=content_publish_service,
            bridge_prefix=settings.bridge_display_prefix,
            bridge_policy_service=bridge_policy_service,
        )
        community_runtime = CommunityRuntime(
            database=database,
            content_publish_service=content_publish_service,
        )
        personal_feed_service = PersonalFeedService(database)
        user_ban_service = UserBanService(database=database, settings=settings)
        personal_feed_publisher = PersonalFeedPublisher(
            database=database,
            personal_feed_service=personal_feed_service,
            fedify_gateway=fedify_gateway,
            user_ban_service=user_ban_service,
            bridge_policy_service=bridge_policy_service,
            fedify_origin=settings.normalized_fedify_origin,
            delivery_worker=user_actor_delivery_worker,
        )
        personal_feed_conversation_runtime = PersonalFeedConversationRuntime(
            database=database,
            personal_feed_service=personal_feed_service,
            fedify_gateway=fedify_gateway,
            user_ban_service=user_ban_service,
            bridge_policy_service=bridge_policy_service,
            fedify_origin=settings.normalized_fedify_origin,
            delivery_worker=None,
        )
        event_router = DiscordEventRouter(
            database=database,
            community_runtime=community_runtime,
            local_community_runtime=local_community_runtime,
            bridge_policy_service=bridge_policy_service,
            personal_feed_service=personal_feed_service,
            personal_feed_publisher=personal_feed_publisher,
            personal_feed_conversation_runtime=personal_feed_conversation_runtime,
        )
        bot = BridgeBot(
            settings=settings,
            database=database,
            fedify_gateway=fedify_gateway,
            event_router=event_router,
            bridge_policy_service=bridge_policy_service,
            user_profile_service=user_profile_service,
            personal_feed_service=personal_feed_service,
            user_ban_service=user_ban_service,
        )
        # Physical adapter borrows BridgeBot: Runtime owns its admission/drain,
        # while the bot's async context alone closes the Discord SDK.
        connector = ConnectorRef('discord-primary', 'discord')
        adapter_registry = PlatformAdapterRegistry((connector,))
        discord_adapter = DiscordAdapter(connector=connector, bot=bot)
        adapter_registry.register(discord_adapter)
        bot.bind_discord_adapter(discord_adapter)
        personal_feed_discord_delivery_worker = PersonalFeedDiscordDeliveryWorker(
            database=database, adapter=discord_adapter, personal_feed_service=personal_feed_service
        )
        personal_feed_conversation_runtime.bind_delivery_wake(
            personal_feed_discord_delivery_worker
        )
        bot.bind_personal_feed_delivery_worker(personal_feed_discord_delivery_worker)
        mutation_context = DiscordMutationContext(
            bridge_policy_service=bridge_policy_service,
            user_actor_delivery_service=user_actor_delivery_service,
            user_actor_delivery_wake=user_actor_delivery_worker,
            fedify_gateway=fedify_gateway,
        )
        message_mutation_handler = DiscordMessageMutationHandler(
            database=database,
            event_router=event_router,
            discord_fetcher=bot,
            mutation_context_provider=lambda: mutation_context,
        )
        bot.set_message_mutation_handler(message_mutation_handler)
        discord_adapter.bind_ingress_sink(PlatformEventIngress(
            adapter=discord_adapter, mutations=message_mutation_handler,
        ))
        # MULTIPLATFORM_STAGE02_TEMP: Production still binds BridgeBot directly into Discord fanout and runtimes; Stage 03/04 must register a real DiscordAdapter and replace these binds without a default-Discord fallback.
        discord_fanout = DiscordFanout(
            bot=bot,
            mutation_tracker=message_mutation_handler,
            database=database,
            policy_service=bridge_policy_service,
        )
        community_runtime.bind_discord_fanout(discord_fanout)
        community_runtime.bind_discord_client(bot)
        local_community_runtime.bind_discord_client(bot)
        user_actor_follow_service = UserActorFollowService(
            database=database, fedify_gateway=fedify_gateway, bot=bot
        )
        user_actor_delivery_service.bind_discord_message_reader(bot)
        runtime = Runtime(
            settings=settings,
            database=database,
            fedify_gateway=fedify_gateway,
            discord_oauth_client=discord_oauth_client,
            content_publish_service=content_publish_service,
            registration_service=registration_service,
            bridge_policy_service=bridge_policy_service,
            bot=bot,
            discord_adapter=discord_adapter,
            community_runtime=community_runtime,
            local_community_runtime=local_community_runtime,
            profile_media_store=media_store,
            user_profile_service=user_profile_service,
            user_actor_follow_service=user_actor_follow_service,
            user_actor_delivery_service=user_actor_delivery_service,
            user_actor_delivery_worker=user_actor_delivery_worker,
            personal_feed_conversation_runtime=personal_feed_conversation_runtime,
            personal_feed_discord_delivery_worker=personal_feed_discord_delivery_worker,
            deployment_state_lease=deployment_lease,
        )
        _validate_runtime_graph(runtime)
        return runtime
    except Exception:
        # Before Runtime exists, app.py owns every opened closeable.  Close in the
        # reverse ownership order used by Runtime.close() so failed validation or
        # bind errors do not leak the DB engine, HTTP clients, or deployment lease.
        if runtime is not None:
            _close_partial_resource(runtime)
        else:
            for resource in reversed(closeables):
                _close_partial_resource(resource)
        raise


def _validate_runtime_graph(runtime: Runtime) -> None:
    """Fail startup before callbacks run if required production binds are missing."""
    # These guards are local and non-I/O: they prove the remaining true cycles
    # were completed by named bind methods rather than raw app-level mutation.
    runtime.bot._require_message_mutation_handler()
    if runtime.bot.discord_adapter is not runtime.discord_adapter:
        raise RuntimeError("Runtime Discord adapter is not bound to BridgeBot")
    if runtime.discord_adapter._sink is None:
        raise RuntimeError("Discord normalized ingress is not configured")
    runtime.bot._require_personal_feed_delivery_worker()
    if runtime.community_runtime.discord_fanout is None:
        raise RuntimeError("CommunityRuntime Discord fanout is not configured")
    if runtime.community_runtime.bot is None:
        raise RuntimeError("CommunityRuntime Discord client is not configured")
    if runtime.local_community_runtime.bot is None:
        raise RuntimeError("LocalCommunityRuntime Discord client is not configured")
    if runtime.personal_feed_conversation_runtime.delivery_worker is None:
        raise RuntimeError("Personal-feed conversation delivery wake is not configured")
    if runtime.user_actor_delivery_service.discord_message_reader is None:
        raise RuntimeError("User actor delivery Discord reader is not configured")


def _close_partial_resource(resource: object) -> None:
    """Close one partially constructed owner from synchronous startup failure code."""
    close = getattr(resource, "close", None)
    if close is None and hasattr(resource, "deployment_state_lease"):
        # A complete Runtime reuses its normal close contract so worker and client
        # ordering stays identical even if final validation fails after creation.
        _run_coroutine_synchronously(resource.close())
        return
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        _run_coroutine_synchronously(result)


def _run_coroutine_synchronously(awaitable: object) -> None:
    """Await cleanup from sync startup code, including when an event loop exists."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(awaitable)
        return

    error: list[BaseException] = []

    def runner() -> None:
        """Run async cleanup in a temporary loop without blocking the active loop API."""
        try:
            asyncio.run(awaitable)
        except BaseException as exc:  # pragma: no cover - re-raised in caller thread.
            error.append(exc)

    thread = threading.Thread(target=runner, name="partial-startup-cleanup")
    thread.start()
    thread.join()
    if error:
        raise error[0]


if __name__ == "__main__":
    asyncio.run(main())
