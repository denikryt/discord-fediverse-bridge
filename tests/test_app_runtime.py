"""Production runtime composition regression coverage."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock

import pytest

from src.app import build_runtime
from src.config import Settings


def _settings(state_root: str) -> Settings:
    """Build valid isolated production settings under the required durable-state root."""
    return Settings.model_validate(
        {
            "DISCORD_TOKEN": "test-discord-token",
            "DATABASE_URL": f"sqlite:///{state_root}/bridge.db",
            "PUBLIC_BASE_URL": "https://bridge.example",
            "BRIDGE_GATEWAY_URL": "http://127.0.0.1:3000",
            "FEDIFY_SHARED_SECRET": "test-shared-secret",
            "BRIDGE_SUPER_ADMIN_USER_IDS": "1234567890",
            "PROFILE_MEDIA_ROOT": f"{state_root}/profile-media",
            "DEPLOYMENT_STATE_LOCK_PATH": f"{state_root}/deployment-state.lock",
            "PROFILE_MEDIA_LOCK_PATH": f"{state_root}/profile-media.lock",
        }
    )


@pytest.mark.asyncio
async def test_build_runtime_wires_user_actor_follow_service_after_bot_and_closes_owners() -> None:
    Path("/data").mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="stage3-runtime-", dir="/data") as state_root:
        runtime = build_runtime(_settings(state_root))

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
