from __future__ import annotations

import asyncio
import logging

import uvicorn

from .config import Settings
from .db import Database
from .discord_bot import BridgeBot
from .http_api import create_http_app
from .lemmy_client import LemmyClient
from .logging_setup import configure_logging
from .runtime import Runtime

logger = logging.getLogger(__name__)


async def _seed_legacy_subscription(settings: Settings, database: Database, lemmy: LemmyClient) -> None:
    # Backward-compat migration: if the old single-pair env vars are still set,
    # insert a subscription row on the first startup so existing deployments keep
    # working without requiring a manual /subscribe-channel call.
    # The check is idempotent — it does nothing if the subscription already exists.
    channel_id = settings.discord_forum_channel_id
    actor_id = settings.lemmy_community_actor_id
    community_name = settings.lemmy_community_name

    if channel_id is None or actor_id is None:
        return

    existing = database.get_subscription_by_channel(channel_id)
    if existing is not None:
        return

    numeric_id: int | None = None
    if community_name:
        try:
            numeric_id = await lemmy.resolve_community_id(name=community_name)
        except Exception:
            # A resolution failure is not fatal — the subscription is still created
            # without a numeric ID. Outbound posts from this channel will be skipped
            # until the subscription is recreated with a valid community.
            logger.warning("Could not resolve legacy community ID for name '%s'", community_name)

    database.create_subscription(
        discord_channel_id=channel_id,
        lemmy_community_actor_id=actor_id,
        lemmy_community_name=community_name,
        lemmy_community_id=numeric_id,
    )
    logger.info(
        "Created default subscription from legacy config: channel %s -> %s. "
        "You can remove DISCORD_FORUM_CHANNEL_ID, LEMMY_COMMUNITY_NAME, and LEMMY_COMMUNITY_ACTOR_ID from your .env.",
        channel_id,
        actor_id,
    )


async def main() -> None:
    # Build the full runtime once and then run both long-lived entry points
    # against the same shared state.
    settings = Settings()
    configure_logging(settings.log_level)

    database = Database(settings.database_url)
    database.create_all()

    lemmy = LemmyClient(
        settings.normalized_lemmy_base_url,
        settings.lemmy_username_or_email,
        settings.lemmy_password,
    )
    await lemmy.login()
    await _seed_legacy_subscription(settings, database, lemmy)

    bot = BridgeBot(
        settings=settings,
        database=database,
        lemmy=lemmy,
    )
    runtime = Runtime(
        settings=settings,
        database=database,
        lemmy=lemmy,
        bot=bot,
    )
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
