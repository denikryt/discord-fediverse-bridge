"""Runtime builders shared across scenario tests."""

from __future__ import annotations

from pathlib import Path
from src.config import Settings
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.bridge_policy import BridgePolicyService
from src.community_sync.runtime import CommunityRuntime
from src.db import Database
from src.content_publish_service import ContentPublishService


def build_test_policy_service(
    database: Database, settings: object | None = None
) -> BridgePolicyService:
    """Build the explicit policy dependency used by scenario tests."""
    return BridgePolicyService(
        settings=settings or SimpleNamespace(),
        repository=database.bridge_policy_entries,
    )


def build_publish_service(
    database: Database, fedify_gateway: object | None = None
) -> ContentPublishService:
    """Build a publish service with a stable fake gateway by default."""
    return ContentPublishService(
        database=database,
        fedify_gateway=fedify_gateway or AsyncMock(),
        bridge_prefix="[bridge]",
        bridge_policy_service=build_test_policy_service(database),
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
        content_publish_service=build_publish_service(database, fedify_gateway),
        discord_fanout=discord_fanout,
        bot=bot,
    )


def build_runtime_namespace(**kwargs: object) -> SimpleNamespace:
    """Build one lightweight runtime namespace for HTTP/handler tests."""
    from bridge_test_support.scenario_graph import wire
    return wire(SimpleNamespace(**kwargs))


def runtime_settings(state_root: Path) -> Settings:
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
