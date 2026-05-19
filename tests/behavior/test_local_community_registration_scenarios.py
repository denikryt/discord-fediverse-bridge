"""Behavior scenarios for local-community registration and command policy."""

from __future__ import annotations

from pathlib import Path

from src.config import Settings
from src.local_communities.service import LocalCommunityService
from src.operations.create_community import (
    CreateCommunityInput,
    create_community_operation,
)
from support.db import build_database


def _settings(*, allowlist: str = "123") -> Settings:
    """Build one Settings instance with the local-community operator allowlist."""
    return Settings.model_construct(
        discord_token="discord-token",
        database_url="sqlite:///./bridge.db",
        fedify_gateway_url="http://127.0.0.1:3000",
        internal_http_host="127.0.0.1",
        internal_http_port=8080,
        public_bridge_base_url="http://127.0.0.1:8080",
        discord_oauth_client_id="",
        discord_oauth_client_secret="",
        discord_oauth_redirect_uri="http://127.0.0.1:8080/auth/discord/callback",
        registration_session_cookie_name="bridge_registration_session",
        registration_session_ttl_seconds=3600,
        fedify_shared_secret="secret",
        fedify_origin="https://bridge.example",
        bridge_display_prefix="[bridge]",
        log_level="INFO",
        federation_allowlist=[],
        local_community_operator_allowlist=allowlist.split(",") if allowlist else [],
    )


def test_allowlisted_operator_creates_local_community_and_persists_actor_metadata(
    tmp_path: Path,
) -> None:
    """An allowlisted operator should create one persisted local community row."""
    database = build_database(tmp_path, "local-community-registration.db")
    result = create_community_operation(
        CreateCommunityInput(
            database=database,
            settings=_settings(),
            discord_user_id="123",
            discord_guild_id=10,
            discord_forum_channel_id=100,
            slug="hackers",
            name="Hackers",
            description="A local hackerspace forum.",
        )
    )
    created = database.get_local_community_by_slug("hackers")

    assert result.applied is True
    assert created is not None
    assert created.display_name == "Hackers"
    assert created.summary == "A local hackerspace forum."
    assert created.actor_url == "https://bridge.example/communities/hackers"


def test_non_allowlisted_operator_cannot_create_local_community(
    tmp_path: Path,
) -> None:
    """A non-allowlisted operator should not be able to create a local community."""
    database = build_database(tmp_path, "local-community-registration-denied.db")
    result = create_community_operation(
        CreateCommunityInput(
            database=database,
            settings=_settings(allowlist="999"),
            discord_user_id="123",
            discord_guild_id=10,
            discord_forum_channel_id=100,
            slug="hackers",
            name="Hackers",
            description="A local hackerspace forum.",
        )
    )

    assert result.applied is False
    assert result.reason == "operator_not_allowlisted"
    assert database.get_local_community_by_slug("hackers") is None


def test_service_rejects_duplicate_forum_binding(
    tmp_path: Path,
) -> None:
    """A forum channel already bound to one local community cannot be reused."""
    database = build_database(tmp_path, "local-community-registration-duplicate.db")
    service = LocalCommunityService(database=database, base_url="https://bridge.example")
    service.create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="hackers",
        name="Hackers",
        description="A local hackerspace forum.",
    )

    try:
        service.create_local_community(
            discord_guild_id=10,
            discord_forum_channel_id=100,
            slug="makers",
            name="Makers",
            description="Another forum.",
        )
    except Exception as exc:
        assert "already bound" in str(exc)
    else:
        raise AssertionError("Expected duplicate forum binding to fail")
