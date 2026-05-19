"""Registration flow support builders for HTTP scenario tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.db import Database
from src.http_api import create_http_app
from src.registration_service import RegistrationService
from support.db import build_database


class FakeDiscordOAuthClient:
    """Provide deterministic OAuth behavior without real Discord HTTP calls."""

    def __init__(
        self,
        *,
        authorize_domain: str = "discord.example",
        user_id: str = "1234567890",
        username: str = "denchik",
        avatar_url: str = "https://cdn.discord.example/avatar.png",
    ) -> None:
        """Store one stable fake Discord identity for registration tests."""
        self.authorize_domain = authorize_domain
        self.user_id = user_id
        self.username = username
        self.avatar_url = avatar_url
        self.last_state: str | None = None

    def build_authorization_url(self, state: str) -> str:
        """Return a stable fake Discord authorize URL for redirect assertions."""
        self.last_state = state
        return f"https://{self.authorize_domain}/oauth?state={state}"

    async def exchange_code_for_access_token(self, code: str) -> str:
        """Return one fake token so the callback path can continue."""
        assert code == "oauth-code"
        return "discord-access-token"

    async def fetch_user_profile(self, access_token: str) -> SimpleNamespace:
        """Return one fake Discord user profile for registration scenarios."""
        assert access_token == "discord-access-token"
        return SimpleNamespace(
            user_id=self.user_id,
            username=self.username,
            avatar_url=self.avatar_url,
        )


def build_registration_runtime(
    tmp_path: Path,
    *,
    db_name: str,
    base_url: str,
    fedify_origin: str | None = None,
) -> SimpleNamespace:
    """Build the shared runtime dependencies used by registration route tests."""
    database: Database = build_database(tmp_path, db_name)
    settings = SimpleNamespace(
        fedify_shared_secret="test-secret",
        normalized_public_bridge_base_url=base_url,
        normalized_fedify_origin=fedify_origin or base_url,
        registration_session_cookie_name="bridge_registration_session",
        registration_session_ttl_seconds=3600,
    )
    registration_service = RegistrationService(
        database=database,
        base_url=settings.normalized_fedify_origin,
        keypair_generator=lambda: ("test-public-key", "test-private-key"),
    )
    return SimpleNamespace(
        settings=settings,
        database=database,
        registration_service=registration_service,
        discord_oauth_client=FakeDiscordOAuthClient(),
        fedify_gateway=SimpleNamespace(),
        bot=SimpleNamespace(),
    )


def build_registration_client(
    tmp_path: Path,
    *,
    db_name: str,
    base_url: str,
    fedify_origin: str | None = None,
) -> tuple[TestClient, Database]:
    """Build a TestClient and its backing database for registration tests."""
    runtime = build_registration_runtime(
        tmp_path,
        db_name=db_name,
        base_url=base_url,
        fedify_origin=fedify_origin,
    )
    return TestClient(create_http_app(runtime)), runtime.database
