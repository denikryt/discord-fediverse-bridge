"""Runtime registration scenarios for the Stage 5 FastAPI backend."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.db import Database
from src.http_api import create_http_app
from src.registration_service import RegistrationService


class FakeDiscordOAuthClient:
    """Provide deterministic OAuth behavior without real Discord HTTP calls."""

    def __init__(self) -> None:
        self.last_state: str | None = None

    def build_authorization_url(self, state: str) -> str:
        """Return a stable fake Discord authorize URL for redirect assertions."""
        self.last_state = state
        return f"https://discord.example/oauth?state={state}"

    async def exchange_code_for_access_token(self, code: str) -> str:
        """Return a fake token so the callback path can continue."""
        assert code == "oauth-code"
        return "discord-access-token"

    async def fetch_user_profile(self, access_token: str) -> SimpleNamespace:
        """Return one fake Discord user profile for registration scenarios."""
        assert access_token == "discord-access-token"
        return SimpleNamespace(
            user_id="1234567890",
            username="denchik",
            avatar_url="https://cdn.discord.example/avatar.png",
        )


def _database(tmp_path: Path) -> Database:
    """Create a real SQLite-backed repository for registration flow tests."""
    database = Database(f"sqlite:///{tmp_path / 'bridge-stage5.db'}")
    database.create_all()
    return database


def _runtime(tmp_path: Path) -> SimpleNamespace:
    """Build the shared runtime dependencies used by registration route tests."""
    database = _database(tmp_path)
    settings = SimpleNamespace(
        fedify_shared_secret="test-secret",
        normalized_public_bridge_base_url="https://bridge.example.com",
        normalized_fedify_origin="https://gateway.example.com",
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
        lemmy=SimpleNamespace(),
        fedify_gateway=SimpleNamespace(),
        bot=SimpleNamespace(),
    )


def _client(tmp_path: Path) -> tuple[TestClient, Database]:
    """Create a TestClient plus the shared database for one scenario."""
    runtime = _runtime(tmp_path)
    return TestClient(create_http_app(runtime)), runtime.database


def test_register_page_returns_simple_html(tmp_path: Path) -> None:
    """The entry page should render without requiring any OAuth state first."""
    client, _database = _client(tmp_path)

    response = client.get("/register")

    assert response.status_code == 200
    assert "Continue with Discord" in response.text


def test_auth_start_creates_session_and_redirects_to_discord(tmp_path: Path) -> None:
    """OAuth start should persist state server-side before redirecting away."""
    client, database = _client(tmp_path)

    client.get("/register")
    response = client.get("/auth/discord/start", follow_redirects=False)

    session_token = client.cookies.get("bridge_registration_session")
    session = database.get_registration_session_by_token(session_token)

    assert response.status_code == 307
    assert response.headers["location"].startswith("https://discord.example/oauth")
    assert session is not None
    assert session.oauth_state is not None


def test_callback_rejects_wrong_state_without_creating_user(tmp_path: Path) -> None:
    """State mismatches must fail before any Discord identity is trusted."""
    client, database = _client(tmp_path)

    client.get("/register")
    client.get("/auth/discord/start", follow_redirects=False)
    response = client.get(
        "/auth/discord/callback?code=oauth-code&state=wrong-state",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert database.get_user_by_discord_user_id("1234567890") is None


def test_callback_success_stores_discord_identity_in_session(tmp_path: Path) -> None:
    """A valid callback should persist the authenticated Discord user."""
    client, database = _client(tmp_path)

    client.get("/register")
    client.get("/auth/discord/start", follow_redirects=False)
    session_token = client.cookies.get("bridge_registration_session")
    session = database.get_registration_session_by_token(session_token)
    assert session is not None

    response = client.get(
        f"/auth/discord/callback?code=oauth-code&state={session.oauth_state}",
        follow_redirects=False,
    )
    updated = database.get_registration_session_by_token(session_token)

    assert response.status_code == 307
    assert response.headers["location"] == "/register"
    assert updated is not None
    assert updated.discord_user_id == "1234567890"
    assert updated.discord_username == "denchik"


def test_register_complete_creates_user_with_urls_and_keys(tmp_path: Path) -> None:
    """Completing registration should write the shared user actor record."""
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
    created = database.get_user_by_activitypub_username("alice")

    assert response.status_code == 303
    assert response.headers["location"] == "/register/success"
    assert created is not None
    assert created.discord_user_id == "1234567890"
    assert created.actor_url == "https://gateway.example.com/users/alice"
    assert created.inbox_url == "https://gateway.example.com/users/alice/inbox"
    assert created.outbox_url == "https://gateway.example.com/users/alice/outbox"
    assert created.followers_url == "https://gateway.example.com/users/alice/followers"
    assert created.public_key_pem == "test-public-key"
    assert created.private_key_pem == "test-private-key"


def test_register_complete_rejects_reserved_username(tmp_path: Path) -> None:
    """Reserved names should be blocked even after successful Discord auth."""
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
        content="username=bridge",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert "reserved" in response.text.lower()
    assert database.get_user_by_activitypub_username("bridge") is None


def test_register_complete_rejects_invalid_username_syntax(tmp_path: Path) -> None:
    """Username syntax must stay within the local-actor naming contract."""
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
        content="username=Alice!",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert "lowercase letters" in response.text


def test_register_complete_rejects_duplicate_username(tmp_path: Path) -> None:
    """Username uniqueness must be enforced before a second actor is created."""
    client, database = _client(tmp_path)
    database.create_user(
        discord_user_id="existing-user",
        activitypub_username="alice",
        actor_url="https://gateway.example.com/users/alice",
        inbox_url="https://gateway.example.com/users/alice/inbox",
        outbox_url="https://gateway.example.com/users/alice/outbox",
        followers_url="https://gateway.example.com/users/alice/followers",
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
        content="username=alice",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 400
    assert "already taken" in response.text.lower()


def test_register_complete_returns_existing_user_for_duplicate_discord_id(
    tmp_path: Path,
) -> None:
    """A second registration attempt for one Discord ID must reuse the actor."""
    client, database = _client(tmp_path)
    database.create_user(
        discord_user_id="1234567890",
        activitypub_username="alice",
        actor_url="https://gateway.example.com/users/alice",
        inbox_url="https://gateway.example.com/users/alice/inbox",
        outbox_url="https://gateway.example.com/users/alice/outbox",
        followers_url="https://gateway.example.com/users/alice/followers",
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
    assert database.get_user_by_activitypub_username("other-name") is None


def test_register_page_shows_existing_registration_for_repeat_user(tmp_path: Path) -> None:
    """A repeat registration should show the existing actor instead of duplicating it."""
    client, database = _client(tmp_path)
    database.create_user(
        discord_user_id="1234567890",
        activitypub_username="alice",
        actor_url="https://gateway.example.com/users/alice",
        inbox_url="https://gateway.example.com/users/alice/inbox",
        outbox_url="https://gateway.example.com/users/alice/outbox",
        followers_url="https://gateway.example.com/users/alice/followers",
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

    response = client.get("/register")

    assert response.status_code == 200
    assert "Already registered" in response.text
    assert "@alice@gateway.example.com" in response.text
    assert "https://gateway.example.com/users/alice" in response.text


def test_register_success_page_shows_created_handle(tmp_path: Path) -> None:
    """The success page should echo the local handle after completion."""
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
    client.post(
        "/register/complete",
        content="username=alice",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    response = client.get("/register/success")

    assert response.status_code == 200
    assert "@alice@gateway.example.com" in response.text
    assert "https://gateway.example.com/users/alice" in response.text


def test_actor_urls_use_fedify_origin_not_bridge_url() -> None:
    """Actor URLs must point to the fedify gateway domain, not the bridge web domain."""
    service = RegistrationService(
        database=None,  # type: ignore[arg-type]
        base_url="https://gateway.example.com",
        keypair_generator=lambda: ("pub", "priv"),
    )

    actor_url, inbox_url, outbox_url, followers_url = service.build_actor_urls("alice")

    assert actor_url == "https://gateway.example.com/users/alice"
    assert inbox_url == "https://gateway.example.com/users/alice/inbox"
    assert outbox_url == "https://gateway.example.com/users/alice/outbox"
    assert followers_url == "https://gateway.example.com/users/alice/followers"
    # bridge web domain must not appear in any actor URL
    assert "bridge" not in actor_url
