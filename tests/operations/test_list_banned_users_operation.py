"""Observable command behavior tests for `/list-banned-users`."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from src.local_communities.service import LocalCommunityService
from src.operations import ListBannedUsersInput, list_banned_users_operation
from support.db import build_database


def _settings(*, super_admins: list[str] | None = None) -> SimpleNamespace:
    """Build the settings fields read by list operation policy."""
    return SimpleNamespace(local_community_operator_allowlist=super_admins or [])


def _community(database: object, *, slug: str = "cats", guild_id: int = 10, owner_id: str = "111", forum_channel_id: int = 100) -> object:
    """Create one active local community for list scenarios."""
    LocalCommunityService(database=database, base_url="https://bridge.example", keypair_generator=lambda: ("public-key", "private-key")).create_local_community(
        discord_guild_id=guild_id,
        discord_forum_channel_id=forum_channel_id,
        slug=slug,
        name=slug.title(),
        description=f"{slug} community.",
        created_by_discord_user_id=owner_id,
    )
    return database.local_communities.get_local_community_by_slug(slug)


def _ban(database: object, community: object, *, handle: str, reason: str | None, created_at: datetime | None = None) -> object:
    """Create one active ban and optionally force its timestamp."""
    ban = database.community_actor_bans.create_active_ban(
        local_community_id=community.id,
        actor_handle=handle,
        actor_url="https://example.com/u/hidden",
        created_by_discord_user_id="moderator-id",
        reason=reason,
    )
    if created_at is not None:
        with database.session() as session:
            persisted = session.merge(ban)
            persisted.created_at = created_at
            persisted.updated_at = created_at
        ban = database.community_actor_bans.get_active_ban_by_handle(local_community_id=community.id, actor_handle=handle)
    return ban


def test_public_caller_lists_current_guild_active_bans_with_reasons(tmp_path: Path) -> None:
    """Any user can list active bans for a current-guild community privately."""
    database = build_database(tmp_path, "list-bans.db")
    community = _community(database)
    _ban(database, community, handle="alice@example.com", reason="spam")
    _ban(database, community, handle="bob@example.org", reason=None)

    result = list_banned_users_operation(
        ListBannedUsersInput(database, _settings(), "ordinary", 10, "cats")
    )

    assert result.applied is True
    assert "Banned users in community cats:" in result.message
    assert "alice@example.com — spam" in result.message
    assert "bob@example.org — reason not specified" in result.message
    assert "https://example.com" not in result.message
    assert "moderator-id" not in result.message


def test_empty_active_list_returns_empty_message(tmp_path: Path) -> None:
    """Communities with no active bans return the exact empty-list response."""
    database = build_database(tmp_path, "list-empty.db")
    _community(database)

    result = list_banned_users_operation(
        ListBannedUsersInput(database, _settings(), "ordinary", 10, "cats")
    )

    assert result.applied is True
    assert result.reason == "empty"
    assert result.message == "Community cats has no active bans."


def test_inactive_rows_are_excluded_and_rows_are_newest_first(tmp_path: Path) -> None:
    """List output includes active bans only in created_at DESC order."""
    database = build_database(tmp_path, "list-order.db")
    community = _community(database)
    base = datetime(2026, 5, 30, tzinfo=timezone.utc)
    _ban(database, community, handle="old@example.com", reason="old", created_at=base)
    inactive = _ban(database, community, handle="gone@example.com", reason="gone", created_at=base + timedelta(days=1))
    _ban(database, community, handle="new@example.com", reason="new", created_at=base + timedelta(days=2))
    database.community_actor_bans.deactivate_active_ban_by_handle(local_community_id=community.id, actor_handle=inactive.actor_handle)

    result = list_banned_users_operation(
        ListBannedUsersInput(database, _settings(), "ordinary", 10, "cats")
    )

    assert "gone@example.com" not in result.message
    assert result.message.index("new@example.com") < result.message.index("old@example.com")


def test_more_than_twenty_bans_are_truncated(tmp_path: Path) -> None:
    """The v1 list response caps visible rows and reports total count."""
    database = build_database(tmp_path, "list-truncated.db")
    community = _community(database)
    for index in range(25):
        _ban(database, community, handle=f"user{index:02d}@example.com", reason="spam")

    result = list_banned_users_operation(
        ListBannedUsersInput(database, _settings(), "ordinary", 10, "cats")
    )

    assert result.message.count("- user") == 20
    assert "Showing 20 of 25 active bans." in result.message


def test_cross_guild_rejected_for_ordinary_user_but_allowed_for_super_admin(tmp_path: Path) -> None:
    """Normal users are guild-scoped while super-admins can manually list slugs."""
    database = build_database(tmp_path, "list-cross-guild.db")
    community = _community(database, guild_id=20)
    _ban(database, community, handle="alice@example.com", reason="spam")

    ordinary = list_banned_users_operation(
        ListBannedUsersInput(database, _settings(), "ordinary", 10, "cats")
    )
    admin = list_banned_users_operation(
        ListBannedUsersInput(database, _settings(super_admins=["999"]), "999", 10, "cats")
    )

    assert ordinary.reason == "unknown_or_inaccessible_community"
    assert ordinary.message == "Unknown or inaccessible local community: cats"
    assert admin.applied is True
    assert "alice@example.com — spam" in admin.message


def test_unknown_slug_and_no_guild_are_rejected(tmp_path: Path) -> None:
    """List command rejects missing community and DM invocation."""
    database = build_database(tmp_path, "list-errors.db")
    _community(database)

    unknown = list_banned_users_operation(
        ListBannedUsersInput(database, _settings(), "ordinary", 10, "missing")
    )
    no_guild = list_banned_users_operation(
        ListBannedUsersInput(database, _settings(), "ordinary", None, "cats")
    )

    assert unknown.message == "Unknown or inaccessible local community: missing"
    assert no_guild.message == "This command can only be used inside a guild."


def test_inactive_community_is_inaccessible_for_list(tmp_path: Path) -> None:
    """List runtime rejects inactive communities even when the slug exists."""
    database = build_database(tmp_path, "list-inactive-community.db")
    community = _community(database)
    _ban(database, community, handle="alice@example.com", reason="spam")
    with database.session() as session:
        persisted = session.merge(community)
        persisted.status = "inactive"

    result = list_banned_users_operation(
        ListBannedUsersInput(database, _settings(), "ordinary", 10, "cats")
    )

    assert result.reason == "unknown_or_inaccessible_community"
    assert result.message == "Unknown or inaccessible local community: cats"


def test_disabled_community_rejects_list_banned_users(tmp_path: Path) -> None:
    """Disabled communities do not expose ban-list output until re-enabled."""
    database = build_database(tmp_path, "list-disabled.db")
    community = _community(database)
    _ban(database, community, handle="alice@example.com", reason="spam")
    with database.session() as session:
        persisted = session.merge(community)
        persisted.status = "disabled"

    result = list_banned_users_operation(
        ListBannedUsersInput(database, _settings(), "ordinary", 10, "cats")
    )

    assert result.applied is False
    assert result.reason == "community_disabled"
    assert result.message == "Community cats is disabled. Use /edit-community to re-enable it first."
    assert "alice@example.com" not in result.message
