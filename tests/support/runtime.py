"""Runtime builders shared across scenario tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.community_sync.runtime import CommunityRuntime
from src.db import Database
from src.discord_publish_service import DiscordPublishService


def build_publish_service(database: Database, fedify_gateway: object | None = None) -> DiscordPublishService:
    """Build a publish service with a stable fake gateway by default."""
    return DiscordPublishService(
        database=database,
        fedify_gateway=fedify_gateway or AsyncMock(),
        bridge_prefix="[bridge]",
    )


def build_community_runtime(
    database: Database,
    *,
    fedify_gateway: object | None = None,
    discord_fanout: object | None = None,
    bot: object | None = None,
) -> CommunityRuntime:
    """Build a real `CommunityRuntime` for scenario tests."""
    return CommunityRuntime(
        database=database,
        discord_publish_service=build_publish_service(database, fedify_gateway),
        discord_fanout=discord_fanout,
        bot=bot,
    )


def build_runtime_namespace(**kwargs: object) -> SimpleNamespace:
    """Build one lightweight runtime namespace for HTTP/handler tests."""
    return SimpleNamespace(**kwargs)

