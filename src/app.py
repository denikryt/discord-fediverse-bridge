from __future__ import annotations

import asyncio
import logging

import uvicorn

from .community_sync.discord_fanout import DiscordFanout
from .community_sync.runtime import CommunityRuntime
from .config import Settings
from .db import Database
from .discord_bot import BridgeBot
from .discord_oauth_client import DiscordOAuthClient
from .discord_publish_service import DiscordPublishService
from .fedify_gateway_client import FedifyGatewayClient
from .http_api import create_http_app
from .logging_setup import configure_logging
from .registration_service import RegistrationService
from .runtime import Runtime

logger = logging.getLogger(__name__)


async def main() -> None:
    # Build the full runtime once and then run both long-lived entry points
    # against the same shared state.
    settings = Settings()
    configure_logging(settings.log_level)

    database = Database(settings.database_url)
    database.create_all()
    database.migrate()

    fedify_gateway = FedifyGatewayClient(settings)
    discord_oauth_client = DiscordOAuthClient(settings)
    discord_publish_service = DiscordPublishService(
        database=database,
        fedify_gateway=fedify_gateway,
        bridge_prefix=settings.bridge_display_prefix,
    )
    registration_service = RegistrationService(
        database=database,
        base_url=settings.normalized_fedify_origin,
    )

    # CommunityRuntime is constructed without discord_fanout first so it can be
    # passed to BridgeBot. DiscordFanout needs a reference to bot (for
    # fetch_forum_channel), which means bot must exist first. We inject the fanout
    # after construction via attribute assignment to break the circular dependency:
    # CommunityRuntime → DiscordFanout → BridgeBot → CommunityRuntime.
    community_runtime = CommunityRuntime(
        database=database,
        discord_publish_service=discord_publish_service,
    )
    bot = BridgeBot(
        settings=settings,
        database=database,
        fedify_gateway=fedify_gateway,
        community_runtime=community_runtime,
    )
    discord_fanout = DiscordFanout(bot=bot)
    community_runtime.discord_fanout = discord_fanout
    # Wire bot into CommunityRuntime after construction for the same reason
    # discord_fanout is injected late — bot depends on community_runtime existing first.
    community_runtime.bot = bot
    runtime = Runtime(
        settings=settings,
        database=database,
        fedify_gateway=fedify_gateway,
        discord_oauth_client=discord_oauth_client,
        discord_publish_service=discord_publish_service,
        registration_service=registration_service,
        bot=bot,
        community_runtime=community_runtime,
    )
    # Inject runtime into bot so edit/delete handlers can call AP gateway
    bot.set_runtime(runtime)
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


if __name__ == "__main__":
    asyncio.run(main())
