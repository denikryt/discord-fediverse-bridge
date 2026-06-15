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

    oauth_close = AsyncMock(wraps=runtime.discord_oauth_client.close)
    gateway_close = AsyncMock(wraps=runtime.fedify_gateway.close)
    database_close = Mock(wraps=runtime.database.close)
    lease_close = Mock(wraps=runtime.deployment_state_lease.close)
    runtime.discord_oauth_client.close = oauth_close
    runtime.fedify_gateway.close = gateway_close
    runtime.database.close = database_close
    runtime.deployment_state_lease.close = lease_close

    await runtime.close()

    oauth_close.assert_awaited_once_with()
    gateway_close.assert_awaited_once_with()
    database_close.assert_called_once_with()
    lease_close.assert_called_once_with()
