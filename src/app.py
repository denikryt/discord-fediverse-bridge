from __future__ import annotations

import asyncio

import uvicorn

from .config import Settings
from .db import Database
from .discord_bot import BridgeBot
from .http_api import create_http_app
from .lemmy_client import LemmyClient
from .logging_setup import configure_logging
from .runtime import Runtime


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
    lemmy_community_id = await lemmy.resolve_community_id(name=settings.lemmy_community_name)
    bot = BridgeBot(
        settings=settings,
        database=database,
        lemmy=lemmy,
        lemmy_community_id=lemmy_community_id,
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
