"""Operation-level behavior tests for local-community actor bans."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.operations import BanUserInput, ban_user_operation
from src.local_communities.service import LocalCommunityService
from support.db import build_database


def _settings(*, allowlist: list[str] | None = None) -> SimpleNamespace:
    """Build only the settings fields the operation needs."""
    return SimpleNamespace(local_community_operator_allowlist=allowlist or ["123"])


def _local_community(database: object) -> object:
    """Seed one bridge-hosted community for moderation command scenarios."""
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="cats",
        name="Cats",
        description="Cat photos.",
    )
    return database.local_communities.get_local_community_by_slug("cats")


def test_allowed_operator_bans_remote_handle_by_community_slug(tmp_path: Path) -> None:
    """Allowed operators can create one active community-scoped ban row."""
    database = build_database(tmp_path, "ban-user.db")
    community = _local_community(database)

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(),
            discord_user_id="123",
            community_slug="cats",
            actor_handle="Alice@Example.COM",
            reason="spam",
        )
    )
    ban = database.community_actor_bans.get_active_ban_by_handle(
        local_community_id=community.id,
        actor_handle="Alice@example.com",
    )

    assert result.applied is True
    assert result.message == "Banned Alice@example.com from community cats.\nReason: spam"
    assert ban is not None
    assert ban.actor_handle == "Alice@example.com"
    assert ban.reason == "spam"
    assert ban.created_by_discord_user_id == "123"


def test_unknown_community_slug_is_rejected(tmp_path: Path) -> None:
    """A typoed community slug must not create a global or orphan ban."""
    database = build_database(tmp_path, "ban-user-unknown.db")

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(),
            discord_user_id="123",
            community_slug="missing",
            actor_handle="alice@example.com",
            reason=None,
        )
    )

    assert result.applied is False
    assert result.reason == "unknown_community"
    assert result.message == "Unknown local community slug: missing"


def test_non_allowlisted_operator_is_rejected(tmp_path: Path) -> None:
    """The v1 command uses the same coarse allowlist gate as community creation."""
    database = build_database(tmp_path, "ban-user-denied.db")
    _local_community(database)

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(allowlist=["999"]),
            discord_user_id="123",
            community_slug="cats",
            actor_handle="alice@example.com",
            reason=None,
        )
    )

    assert result.applied is False
    assert result.reason == "operator_not_allowlisted"
    assert result.message == "You are not allowed to ban users from local communities with this bot."


def test_invalid_remote_handle_is_rejected(tmp_path: Path) -> None:
    """The operation rejects ActivityPub URLs as command input in v1."""
    database = build_database(tmp_path, "ban-user-invalid.db")
    _local_community(database)

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(),
            discord_user_id="123",
            community_slug="cats",
            actor_handle="https://example.com/u/alice",
            reason=None,
        )
    )

    assert result.applied is False
    assert result.reason == "invalid_handle"
    assert result.message == "Invalid remote user handle. Use user@example.com."


def test_duplicate_active_ban_reports_existing_reason(tmp_path: Path) -> None:
    """Duplicate active bans must be explicit and must not overwrite the reason."""
    database = build_database(tmp_path, "ban-user-duplicate.db")
    community = _local_community(database)
    database.community_actor_bans.create_active_ban(
        local_community_id=community.id,
        actor_handle="alice@example.com",
        actor_url=None,
        created_by_discord_user_id="123",
        reason="spam",
    )

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(),
            discord_user_id="123",
            community_slug="cats",
            actor_handle="alice@example.com",
            reason="new reason",
        )
    )

    assert result.applied is False
    assert result.reason == "duplicate_active_ban"
    assert result.message == "User alice@example.com is already banned in community cats.\nReason: spam"
