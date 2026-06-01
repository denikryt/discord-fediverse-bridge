"""Observable operation tests for `/edit-community` modal submissions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.local_communities.service import LocalCommunityService
from src.operations import EditCommunityInput, edit_community_operation
from support.db import build_database


def _settings(*, super_admins: list[str] | None = None) -> SimpleNamespace:
    """Build settings fields read by management permission checks."""
    return SimpleNamespace(local_community_operator_allowlist=super_admins or [])


def _community(
    database: object,
    *,
    slug: str = "cats",
    guild_id: int = 10,
    forum_channel_id: int = 100,
    owner_id: str = "111",
    summary: str | None = "Old summary",
) -> object:
    """Create one editable local community for operation scenarios."""
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=guild_id,
        discord_forum_channel_id=forum_channel_id,
        slug=slug,
        name="Cats",
        description=summary,
        created_by_discord_user_id=owner_id,
    )
    return database.local_communities.get_local_community_by_slug(slug)


def _edit(database: object, *, user_id: str = "111", guild_id: int | None = 10, slug: str = "cats", display_name: str = "New Cats", summary: str | None = "New summary", status: str = "active", settings: SimpleNamespace | None = None):
    """Execute the edit operation with default owner/current-guild inputs."""
    return edit_community_operation(
        EditCommunityInput(
            database=database,
            settings=settings or _settings(),
            discord_user_id=user_id,
            discord_guild_id=guild_id,
            community_slug=slug,
            display_name=display_name,
            summary=summary,
            status=status,
        )
    )


def test_owner_updates_display_name_and_summary(tmp_path: Path) -> None:
    """A current-guild owner can update both editable metadata fields."""
    database = build_database(tmp_path, "edit-owner.db")
    _community(database)

    result = _edit(database)
    updated = database.local_communities.get_local_community_by_slug("cats")

    assert result.applied is True
    assert result.reason == "updated"
    assert result.message == (
        "Updated community cats.\n"
        "Display name: New Cats\n"
        "Summary: New summary\n"
        "Status: active\n"
        "New posts, comments, follows, and subscriptions are now allowed."
    )
    assert updated.display_name == "New Cats"
    assert updated.summary == "New summary"


def test_owner_clears_summary_to_null(tmp_path: Path) -> None:
    """Whitespace-only summary clears the optional summary field to NULL."""
    database = build_database(tmp_path, "edit-clear-summary.db")
    _community(database)

    result = _edit(database, summary="   ")
    updated = database.local_communities.get_local_community_by_slug("cats")

    assert result.applied is True
    assert updated.summary is None
    assert result.message == (
        "Updated community cats.\n"
        "Display name: New Cats\n"
        "Summary: not specified\n"
        "Status: active\n"
        "New posts, comments, follows, and subscriptions are now allowed."
    )


def test_unchanged_submit_succeeds(tmp_path: Path) -> None:
    """Saving unchanged modal values is a successful idempotent edit."""
    database = build_database(tmp_path, "edit-unchanged.db")
    _community(database)

    result = _edit(database, display_name="Cats", summary="Old summary")
    updated = database.local_communities.get_local_community_by_slug("cats")

    assert result.applied is True
    assert updated.display_name == "Cats"
    assert updated.summary == "Old summary"


def test_owner_disables_and_reenables_community(tmp_path: Path) -> None:
    """Lifecycle status can be toggled without destroying community state."""
    database = build_database(tmp_path, "edit-status.db")
    _community(database)

    disabled = _edit(database, status="disabled")
    after_disabled = database.local_communities.get_local_community_by_slug("cats")
    enabled = _edit(database, status="active", display_name="New Cats", summary="New summary")
    after_enabled = database.local_communities.get_local_community_by_slug("cats")

    assert disabled.applied is True
    assert after_disabled.status == "disabled"
    assert disabled.message == (
        "Updated community cats.\n"
        "Display name: New Cats\n"
        "Summary: New summary\n"
        "Status: disabled\n"
        "New posts, comments, follows, and subscriptions are now blocked."
    )
    assert enabled.applied is True
    assert after_enabled.status == "active"


def test_invalid_status_does_not_mutate_db(tmp_path: Path) -> None:
    """Only active and disabled are accepted lifecycle values."""
    database = build_database(tmp_path, "edit-invalid-status.db")
    _community(database)

    result = _edit(database, status="archived")
    unchanged = database.local_communities.get_local_community_by_slug("cats")

    assert result.applied is False
    assert result.reason == "invalid_status"
    assert result.message == "Community status must be active or disabled."
    assert unchanged.status == "active"
    assert unchanged.display_name == "Cats"


def test_display_name_and_summary_validation_do_not_mutate_db(tmp_path: Path) -> None:
    """Invalid modal values return stable errors and leave metadata unchanged."""
    database = build_database(tmp_path, "edit-validation.db")
    _community(database)

    empty_name = _edit(database, display_name="   ")
    long_name = _edit(database, display_name="x" * 101)
    long_summary = _edit(database, summary="x" * 1001)
    unchanged = database.local_communities.get_local_community_by_slug("cats")

    assert empty_name.message == "Community display name is required."
    assert long_name.message == "Community display name must be 100 characters or fewer."
    assert long_summary.message == "Community summary must be 1000 characters or fewer."
    assert unchanged.display_name == "Cats"
    assert unchanged.summary == "Old summary"


def test_non_owner_cross_guild_and_unknown_slug_are_rejected(tmp_path: Path) -> None:
    """Access preconditions reject unauthorized, cross-guild, and missing slugs."""
    database = build_database(tmp_path, "edit-access.db")
    _community(database, guild_id=20)

    non_owner = _edit(database, user_id="222", guild_id=20)
    cross_guild_owner = _edit(database, user_id="111", guild_id=10)
    unknown = _edit(database, user_id="111", guild_id=10, slug="missing")
    no_guild = _edit(database, user_id="111", guild_id=None)
    unchanged = database.local_communities.get_local_community_by_slug("cats")

    assert non_owner.reason == "cannot_manage_community"
    assert non_owner.message == "You are not allowed to manage this local community."
    assert cross_guild_owner.reason == "unknown_or_inaccessible_community"
    assert unknown.message == "Unknown or inaccessible local community: missing"
    assert no_guild.message == "This command can only be used inside a guild."
    assert unchanged.display_name == "Cats"
    assert unchanged.summary == "Old summary"


def test_super_admin_edits_cross_guild_manual_slug(tmp_path: Path) -> None:
    """A super-admin can edit an active community from another guild context."""
    database = build_database(tmp_path, "edit-admin-cross-guild.db")
    _community(database, guild_id=20, owner_id="111")

    result = _edit(
        database,
        user_id="999",
        guild_id=10,
        settings=_settings(super_admins=["999"]),
        display_name="Admin Cats",
        summary=None,
    )
    updated = database.local_communities.get_local_community_by_slug("cats")

    assert result.applied is True
    assert updated.display_name == "Admin Cats"
    assert updated.summary is None


def test_inactive_or_deleted_community_rejected_on_submit(tmp_path: Path) -> None:
    """Submit path re-checks current row state before writing metadata."""
    database = build_database(tmp_path, "edit-submit-recheck.db")
    community = _community(database)
    with database.session() as session:
        persisted = session.merge(community)
        persisted.status = "inactive"

    result = _edit(database)
    unchanged = database.local_communities.get_local_community_by_slug("cats")

    assert result.reason == "unknown_or_inaccessible_community"
    assert result.message == "Unknown or inaccessible local community: cats"
    assert unchanged.display_name == "Cats"
