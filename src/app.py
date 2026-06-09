"""Process startup and runtime wiring for the Discord bridge and FastAPI server."""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from .actor_key_service import BridgeActorKeyBootstrap
from .bridge_policy import BridgePolicyService
from .community_sync.discord_fanout import DiscordFanout
from .community_sync.runtime import CommunityRuntime
from .config import Settings
from .db import Database
from .discord_bot import BridgeBot
from .discord_event_router import DiscordEventRouter
from .discord_oauth_client import DiscordOAuthClient
from .content_publish_service import ContentPublishService
from .fedify_gateway_client import FedifyGatewayClient
from .http_api import create_http_app
from .local_communities.runtime import LocalCommunityRuntime
from .logging_setup import configure_logging
from .registration_service import RegistrationService
from .project_version import APP_VERSION
from .runtime import Runtime

logger = logging.getLogger(__name__)


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
        # The Discord client owns connection startup/shutdown through its async context.
        async with bot:
            await bot.start(settings.discord_token)

    async def run_http_server() -> None:
        await http_server.serve()

    async with asyncio.TaskGroup() as task_group:
        # Both tasks are mandatory for the bridge to function, so they share one lifecycle.
        task_group.create_task(run_bot(), name="discord-bot")
        task_group.create_task(run_http_server(), name="internal-http-server")


def build_runtime(settings: Settings) -> Runtime:
    """Construct the full long-lived runtime for both bridge modes.

    Startup wiring lives here instead of inline in `main()` so adding new
    runtimes does not hide lifecycle composition inside one large bootstrap
    function.
    """

    database = Database(settings.database_url)
    database.create_all()
    database.migrate()
    BridgeActorKeyBootstrap(database=database, settings=settings).ensure()

    bridge_policy_service = BridgePolicyService(settings=settings, repository=database.bridge_policy_entries)
    fedify_gateway = FedifyGatewayClient(settings)
    discord_oauth_client = DiscordOAuthClient(settings)
    content_publish_service = ContentPublishService(
        database=database,
        fedify_gateway=fedify_gateway,
        bridge_prefix=settings.bridge_display_prefix,
        settings=settings,
        bridge_policy_service=bridge_policy_service,
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

    # CommunityRuntime is constructed without discord_fanout first so it can be
    # passed to BridgeBot. DiscordFanout needs a reference to bot (for
    # fetch_forum_channel), which means bot must exist first. We inject the fanout
    # after construction via attribute assignment to break the circular dependency:
    # CommunityRuntime → DiscordFanout → BridgeBot → CommunityRuntime.
    community_runtime = CommunityRuntime(
        database=database,
        content_publish_service=content_publish_service,
    )
    event_router = DiscordEventRouter(
        database=database,
        community_runtime=community_runtime,
        local_community_runtime=local_community_runtime,
        bridge_policy_service=bridge_policy_service,
    )
    bot = BridgeBot(
        settings=settings,
        database=database,
        fedify_gateway=fedify_gateway,
        event_router=event_router,
        bridge_policy_service=bridge_policy_service,
    )
    discord_fanout = DiscordFanout(bot=bot, mutation_tracker=bot, database=database, policy_service=bridge_policy_service)
    community_runtime.discord_fanout = discord_fanout
    # Wire bot into CommunityRuntime after construction for the same reason
    # discord_fanout is injected late — bot depends on community_runtime existing first.
    community_runtime.bot = bot
    local_community_runtime.bot = bot
    runtime = Runtime(
        settings=settings,
        database=database,
        fedify_gateway=fedify_gateway,
        discord_oauth_client=discord_oauth_client,
        content_publish_service=content_publish_service,
        registration_service=registration_service,
        bridge_policy_service=bridge_policy_service,
        bot=bot,
        community_runtime=community_runtime,
        local_community_runtime=local_community_runtime,
    )
    # Inject runtime into bot so edit/delete handlers can call AP gateway
    bot.set_runtime(runtime)
    return runtime


if __name__ == "__main__":
    asyncio.run(main())
