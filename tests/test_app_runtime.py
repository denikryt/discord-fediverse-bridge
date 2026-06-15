"""Production runtime composition regression coverage."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from src.app import build_runtime
from src.config import Settings


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

    assert runtime.user_actor_follow_service.bot is runtime.bot
    assert runtime.bot._runtime is runtime
    assert runtime.user_profile_service.delivery_service is runtime.user_actor_delivery_service
    assert runtime.user_profile_service.delivery_worker is runtime.user_actor_delivery_worker

    close_order: list[str] = []

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
    worker_stop = AsyncMock(side_effect=stop_worker)
    oauth_close = AsyncMock(side_effect=close_oauth)
    gateway_close = AsyncMock(side_effect=close_gateway)
    database_close = Mock(side_effect=close_database)
    lease_close = Mock(side_effect=close_lease)
    runtime.user_actor_delivery_worker.stop = worker_stop
    runtime.discord_oauth_client.close = oauth_close
    runtime.fedify_gateway.close = gateway_close
    runtime.database.close = database_close
    runtime.deployment_state_lease.close = lease_close

    await runtime.close()

    worker_stop.assert_awaited_once_with()
    oauth_close.assert_awaited_once_with()
    gateway_close.assert_awaited_once_with()
    database_close.assert_called_once_with()
    lease_close.assert_called_once_with()
    assert close_order == ["worker", "oauth", "gateway", "database", "lease"]
