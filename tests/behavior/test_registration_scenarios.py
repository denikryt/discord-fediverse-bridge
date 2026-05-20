"""Behavior scenarios for user registration and repeat registration flows."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.commands import register as register_command
from src.db import Database
from src.http_api import create_http_app
from src.registration_service import RegistrationService
from tests_constants import (
    BRIDGE_HOST_DOMAIN,
    DISCORD_CDN_DOMAIN,
    DISCORD_EXAMPLE_DOMAIN,
)


class FakeDiscordOAuthClient:
    """Provide deterministic OAuth redirects and identity data for scenarios."""

    def __init__(self) -> None:
        self.last_state: str | None = None

    def build_authorization_url(self, state: str) -> str:
        """Return one stable authorize URL so redirect assertions stay readable."""
        self.last_state = state
        return f"https://{DISCORD_EXAMPLE_DOMAIN}/oauth?state={state}"

    async def exchange_code_for_access_token(self, code: str) -> str:
        """Return one fake token so the callback can continue without HTTP."""
        assert code == "oauth-code"
        return "discord-access-token"

    async def fetch_user_profile(self, access_token: str) -> SimpleNamespace:
        """Return one fixed Discord identity used across registration scenarios."""
        assert access_token == "discord-access-token"
        return SimpleNamespace(
            user_id="1234567890",
            username="denchik",
            avatar_url=f"https://{DISCORD_CDN_DOMAIN}/avatar.png",
        )


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite repository for registration behavior tests."""
    database = Database(f"sqlite:///{tmp_path / 'behavior-registration.db'}")
    database.create_all()
    return database


def _runtime(tmp_path: Path) -> SimpleNamespace:
    """Build the shared runtime used by registration routes in these scenarios."""
    database = _database(tmp_path)
    settings = SimpleNamespace(
        fedify_shared_secret="test-secret",
        normalized_public_bridge_base_url=f"https://{BRIDGE_HOST_DOMAIN}",
        registration_session_cookie_name="bridge_registration_session",
        registration_session_ttl_seconds=3600,
    )
    registration_service = RegistrationService(
        database=database,
        base_url=settings.normalized_public_bridge_base_url,
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


def _client(tmp_path: Path) -> tuple[TestClient, Database]:
    """Create one route client plus its backing database for one scenario."""
    runtime = _runtime(tmp_path)
    return TestClient(create_http_app(runtime)), runtime.database


@pytest.mark.asyncio
async def test_unregistered_user_register_command_returns_ephemeral_link(
    command_tree,
    interaction,
) -> None:
    """An unregistered user should get only the web registration entrypoint."""
    settings = SimpleNamespace(
        normalized_public_bridge_base_url=f"https://{BRIDGE_HOST_DOMAIN}"
    )

    register_command.register(command_tree, settings)

    command = command_tree.commands["register"]
    await command.callback(interaction)

    interaction.response.send_message.assert_awaited_once_with(
        f"Register your ActivityPub identity here:\nhttps://{BRIDGE_HOST_DOMAIN}/register",
        ephemeral=True,
    )


def test_oauth_success_then_registration_complete_creates_user_actor(
    tmp_path: Path,
) -> None:
    """A valid OAuth round-trip plus username form should create one local actor."""
    client, database = _client(tmp_path)

    client.get("/register")
    client.get("/auth/discord/start", follow_redirects=False)
    session_token = client.cookies.get("bridge_registration_session")
    session = database.get_registration_session_by_token(session_token)
    assert session is not None
    client.get(
        f"/auth/discord/callback?code=oauth-code&state={session.oauth_state}",
        follow_redirects=False,
    )

    response = client.post(
        "/register/complete",
        content="username=alice",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    user = database.get_user_by_activitypub_username("alice")
    success_page = client.get("/register/success")

    assert response.status_code == 303
    assert response.headers["location"] == "/register/success"
    assert user is not None
    assert user.actor_url == f"https://{BRIDGE_HOST_DOMAIN}/actors/alice"
    assert user.inbox_url == f"https://{BRIDGE_HOST_DOMAIN}/actors/alice/inbox"
    assert user.outbox_url == f"https://{BRIDGE_HOST_DOMAIN}/actors/alice/outbox"
    assert user.followers_url == f"https://{BRIDGE_HOST_DOMAIN}/actors/alice/followers"
    assert user.public_key_pem == "test-public-key"
    assert user.private_key_pem == "test-private-key"
    assert success_page.status_code == 200
    assert f"@alice@{BRIDGE_HOST_DOMAIN}" in success_page.text
    assert f"https://{BRIDGE_HOST_DOMAIN}/actors/alice" in success_page.text


def test_duplicate_discord_user_repeat_registration_shows_existing_actor(
    tmp_path: Path,
) -> None:
    """A repeat registration must reuse the original actor instead of forking identity."""
    client, database = _client(tmp_path)
    database.create_user(
        discord_user_id="1234567890",
        activitypub_username="alice",
        actor_url=f"https://{BRIDGE_HOST_DOMAIN}/actors/alice",
        inbox_url=f"https://{BRIDGE_HOST_DOMAIN}/actors/alice/inbox",
        outbox_url=f"https://{BRIDGE_HOST_DOMAIN}/actors/alice/outbox",
        followers_url=f"https://{BRIDGE_HOST_DOMAIN}/actors/alice/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )

    client.get("/register")
    client.get("/auth/discord/start", follow_redirects=False)
    session_token = client.cookies.get("bridge_registration_session")
    session = database.get_registration_session_by_token(session_token)
    assert session is not None
    client.get(
        f"/auth/discord/callback?code=oauth-code&state={session.oauth_state}",
        follow_redirects=False,
    )

    response = client.post(
        "/register/complete",
        content="username=other-name",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    assert "Already registered" in response.text
    assert f"@alice@{BRIDGE_HOST_DOMAIN}" in response.text
    assert database.get_user_by_activitypub_username("other-name") is None


def test_invalid_oauth_state_is_rejected_without_creating_user(
    tmp_path: Path,
) -> None:
    """A forged or stale callback state must stop before any user row is trusted."""
    client, database = _client(tmp_path)

    client.get("/register")
    client.get("/auth/discord/start", follow_redirects=False)
    response = client.get(
        "/auth/discord/callback?code=oauth-code&state=wrong-state",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert database.get_user_by_discord_user_id("1234567890") is None
