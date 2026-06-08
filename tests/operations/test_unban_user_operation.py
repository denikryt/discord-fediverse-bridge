"""Observable command behavior tests for `/unban-user`."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from src.local_communities.service import LocalCommunityService
from src.models import CommunityActorBan
from src.operations import UnbanUserInput, unban_user_operation
from support.db import build_database


def _settings(*, super_admins: list[str] | None = None) -> SimpleNamespace:
    """Build the settings fields read by moderation operations."""
    return SimpleNamespace(local_community_operator_allowlist=super_admins or [])


def _community(
    database: object,
    *,
    slug: str = "cats",
    guild_id: int = 10,
    owner_id: str | None = "111",
    forum_channel_id: int = 100,
) -> object:
    """Create one local community and optionally convert it to legacy NULL owner."""
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=guild_id,
        discord_forum_channel_id=forum_channel_id,
        slug=slug,
        name=slug.title(),
        description=f"{slug} community.",
        created_by_discord_user_id=owner_id or "temporary-owner",
    )
    community = database.local_communities.get_local_community_by_slug(slug)
    if owner_id is None:
        with database.session() as session:
            persisted = session.merge(community)
            persisted.created_by_discord_user_id = None
        community = database.local_communities.get_local_community_by_slug(slug)
    return community


def _active_ban(database: object, community: object, *, handle: str = "alice@example.com", banner: str = "111", reason: str | None = "spam") -> object:
    """Create one active ban row for command scenarios."""
    return database.community_actor_bans.create_active_ban(
        local_community_id=community.id,
        actor_handle=handle,
        actor_url=None,
        created_by_discord_user_id=banner,
        reason=reason,
    )


def _all_bans(database: object) -> list[CommunityActorBan]:
    """Return all moderation ban rows for final-state assertions."""
    with database.session() as session:
        return list(session.scalars(select(CommunityActorBan).order_by(CommunityActorBan.id)))


def test_owner_unbans_active_ban_in_own_community(tmp_path: Path) -> None:
    """A community owner can deactivate an active ban in their guild."""
    database = build_database(tmp_path, "unban-owner.db")
    community = _community(database, owner_id="111")
    original = _active_ban(database, community, banner="999")

    result = unban_user_operation(
        UnbanUserInput(database, _settings(), "111", 10, "cats", "alice@example.com")
    )
    bans = _all_bans(database)

    assert result.applied is True
    assert result.reason == "unbanned"
    assert result.message == "Unbanned alice@example.com from community cats."
    assert len(bans) == 1
    assert bans[0].id == original.id
    assert bans[0].status == "inactive"


def test_super_admin_unbans_someone_elses_community(tmp_path: Path) -> None:
    """A super-admin can unban in a community they do not own."""
    database = build_database(tmp_path, "unban-admin.db")
    community = _community(database, owner_id="111")
    _active_ban(database, community, banner="111")

    result = unban_user_operation(
        UnbanUserInput(database, _settings(super_admins=["999"]), "999", 10, "cats", "alice@example.com")
    )

    assert result.applied is True
    assert _all_bans(database)[0].status == "inactive"


def test_super_admin_unbans_cross_guild_manual_slug(tmp_path: Path) -> None:
    """Super-admins may manually manage globally unique slugs across guilds."""
    database = build_database(tmp_path, "unban-admin-cross-guild.db")
    community = _community(database, slug="cats", guild_id=20, owner_id="111")
    _active_ban(database, community)

    result = unban_user_operation(
        UnbanUserInput(database, _settings(super_admins=["999"]), "999", 10, "cats", "alice@example.com")
    )

    assert result.applied is True
    assert _all_bans(database)[0].status == "inactive"


def test_non_owner_is_rejected_and_ban_stays_active(tmp_path: Path) -> None:
    """An unrelated user cannot deactivate another community's ban."""
    database = build_database(tmp_path, "unban-denied.db")
    community = _community(database, owner_id="111")
    _active_ban(database, community)

    result = unban_user_operation(
        UnbanUserInput(database, _settings(), "222", 10, "cats", "alice@example.com")
    )

    assert result.applied is False
    assert result.reason == "cannot_manage_community"
    assert result.message == "You are not allowed to manage this local community."
    assert _all_bans(database)[0].status == "active"


def test_owner_cross_guild_slug_is_unknown_or_inaccessible(tmp_path: Path) -> None:
    """A non-admin owner cannot manage their community from another guild."""
    database = build_database(tmp_path, "unban-owner-cross-guild.db")
    community = _community(database, slug="cats", guild_id=20, owner_id="111")
    _active_ban(database, community)

    result = unban_user_operation(
        UnbanUserInput(database, _settings(), "111", 10, "cats", "alice@example.com")
    )

    assert result.applied is False
    assert result.reason == "unknown_or_inaccessible_community"
    assert result.message == "Unknown or inaccessible local community: cats"
    assert _all_bans(database)[0].status == "active"


def test_legacy_null_owner_can_be_unbanned_only_by_super_admin(tmp_path: Path) -> None:
    """Legacy NULL-owned rows remain manageable only through super-admins."""
    database = build_database(tmp_path, "unban-legacy.db")
    community = _community(database, owner_id=None)
    _active_ban(database, community)

    ordinary = unban_user_operation(
        UnbanUserInput(database, _settings(), "111", 10, "cats", "alice@example.com")
    )
    admin = unban_user_operation(
        UnbanUserInput(database, _settings(super_admins=["999"]), "999", 10, "cats", "alice@example.com")
    )

    assert ordinary.applied is False
    assert ordinary.reason == "cannot_manage_community"
    assert admin.applied is True
    assert _all_bans(database)[0].status == "inactive"


def test_unknown_slug_and_invalid_handle_do_not_change_rows(tmp_path: Path) -> None:
    """Missing communities and malformed handles are rejected without mutation."""
    database = build_database(tmp_path, "unban-invalid.db")
    community = _community(database, owner_id="111")
    _active_ban(database, community)

    unknown = unban_user_operation(
        UnbanUserInput(database, _settings(), "111", 10, "missing", "not-a-handle")
    )
    invalid = unban_user_operation(
        UnbanUserInput(database, _settings(), "111", 10, "cats", "https://example.com/u/alice")
    )

    assert unknown.message == "Unknown or inaccessible local community: missing"
    assert invalid.message == "Invalid remote user handle. Use user@example.com."
    assert [ban.status for ban in _all_bans(database)] == ["active"]


def test_unauthorized_invalid_handle_rejects_permission_first(tmp_path: Path) -> None:
    """Authorization must short-circuit before handle validation feedback."""
    database = build_database(tmp_path, "unban-short-circuit.db")
    community = _community(database, owner_id="111")
    _active_ban(database, community)

    result = unban_user_operation(
        UnbanUserInput(database, _settings(), "222", 10, "cats", "not-a-handle")
    )

    assert result.reason == "cannot_manage_community"
    assert result.message == "You are not allowed to manage this local community."
    assert _all_bans(database)[0].status == "active"


def test_no_active_ban_error_is_generic_for_missing_and_inactive_rows(tmp_path: Path) -> None:
    """The no-active-ban response must not reveal inactive historical state."""
    database = build_database(tmp_path, "unban-no-active.db")
    community = _community(database, owner_id="111")
    inactive = _active_ban(database, community, handle="bob@example.com")
    database.community_actor_bans.deactivate_active_ban_by_handle(
        local_community_id=community.id,
        actor_handle="bob@example.com",
    )

    missing = unban_user_operation(
        UnbanUserInput(database, _settings(), "111", 10, "cats", "alice@example.com")
    )
    inactive_result = unban_user_operation(
        UnbanUserInput(database, _settings(), "111", 10, "cats", "bob@example.com")
    )

    assert missing.message == "User alice@example.com is not actively banned in community cats."
    assert inactive_result.message == "User bob@example.com is not actively banned in community cats."
    assert _all_bans(database)[0].id == inactive.id


def test_no_guild_context_is_rejected_before_community_lookup(tmp_path: Path) -> None:
    """DM invocation is rejected and leaves moderation state unchanged."""
    database = build_database(tmp_path, "unban-no-guild.db")
    community = _community(database, owner_id="111")
    _active_ban(database, community)

    result = unban_user_operation(
        UnbanUserInput(database, _settings(), "111", None, "cats", "alice@example.com")
    )

    assert result.reason == "missing_guild_context"
    assert result.message == "This command can only be used inside a guild."
    assert _all_bans(database)[0].status == "active"


def test_inactive_community_is_inaccessible_for_unban(tmp_path: Path) -> None:
    """Unban runtime rejects inactive local communities even for the owner."""
    database = build_database(tmp_path, "unban-inactive-community.db")
    community = _community(database, owner_id="111")
    _active_ban(database, community)
    with database.session() as session:
        persisted = session.merge(community)
        persisted.status = "inactive"

    result = unban_user_operation(
        UnbanUserInput(database, _settings(), "111", 10, "cats", "alice@example.com")
    )

    assert result.reason == "unknown_or_inaccessible_community"
    assert _all_bans(database)[0].status == "active"


def test_disabled_community_rejects_unban_without_changing_ban(tmp_path: Path) -> None:
    """Disabled communities cannot be changed by `/unban-user`."""
    database = build_database(tmp_path, "unban-disabled.db")
    community = _community(database, owner_id="111")
    _active_ban(database, community)
    with database.session() as session:
        persisted = session.merge(community)
        persisted.status = "disabled"

    result = unban_user_operation(
        UnbanUserInput(database, _settings(), "111", 10, "cats", "alice@example.com")
    )

    assert result.applied is False
    assert result.reason == "community_disabled"
    assert result.message == "Community cats is disabled. Use /edit-community to re-enable it first."
    assert _all_bans(database)[0].status == "active"


def test_global_unban_skips_local_community_repository(tmp_path: Path, monkeypatch) -> None:
    """A global unban deactivates its row without resolving any community."""
    database = build_database(tmp_path, "unban-global-no-community-read.db")
    database.community_actor_bans.create_active_ban(
        local_community_id=None,
        actor_handle="alice@example.com",
        actor_url=None,
        created_by_discord_user_id="999",
        reason="spam",
    )

    def fail_lookup(_: str) -> object:
        """Fail if global scope accidentally touches local-community storage."""
        raise AssertionError("global scope must not query local communities")

    monkeypatch.setattr(
        database.local_communities,
        "get_local_community_by_slug",
        fail_lookup,
    )

    result = unban_user_operation(
        UnbanUserInput(
            database,
            _settings(super_admins=["999"]),
            "999",
            None,
            None,
            "alice@example.com",
        )
    )

    assert result.applied is True
    assert result.reason == "unbanned"
    assert _all_bans(database)[0].status == "inactive"
