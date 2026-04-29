from __future__ import annotations

import asyncio

from .config import Settings
from .db import Database
from .discord_bot import BridgeBot
from .lemmy_client import LemmyClient
from .logging_setup import configure_logging


async def main() -> None:
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
    async with bot:
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
