"""Observable command behavior tests for local-community actor bans."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from src.local_communities.service import LocalCommunityService
from src.models import CommunityActorBan
from src.operations import BanUserInput, ban_user_operation
from support.db import build_database


def _settings(*, super_admins: list[str] | None = None) -> SimpleNamespace:
    """Build only the settings fields the ban operation reads."""
    return SimpleNamespace(local_community_operator_allowlist=super_admins or [])


def _local_community(
    database: object,
    *,
    slug: str = "cats",
    owner_id: str | None = "111",
    forum_channel_id: int = 100,
) -> object:
    """Seed one bridge-hosted community for moderation command scenarios."""
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=forum_channel_id,
        slug=slug,
        name=slug.title(),
        description=f"{slug} community.",
        created_by_discord_user_id=owner_id or "temporary-owner",
    )
    community = database.local_communities.get_local_community_by_slug(slug)
    if owner_id is None:
        # Legacy rows are represented by NULL creator ids after the migration.
        # Tests set this explicitly because new service-created rows must always
        # record the creator id.
        with database.session() as session:
            persisted = session.merge(community)
            persisted.created_by_discord_user_id = None
        community = database.local_communities.get_local_community_by_slug(slug)
    return community


def _ban_count(database: object) -> int:
    """Return the number of persisted community actor ban rows."""
    with database.session() as session:
        return len(list(session.scalars(select(CommunityActorBan))))


def test_owner_bans_remote_handle_in_own_community(tmp_path: Path) -> None:
    """A non-admin owner can create a scoped ban in their own community."""
    database = build_database(tmp_path, "ban-user-owner.db")
    community = _local_community(database, owner_id="111")

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=[]),
            discord_user_id="111",
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
    assert result.reason == "created"
    assert result.message == "Banned Alice@example.com from community cats.\nReason: spam"
    assert ban is not None
    assert ban.actor_handle == "Alice@example.com"
    assert ban.reason == "spam"
    assert ban.created_by_discord_user_id == "111"


def test_super_admin_bans_in_someone_elses_community(tmp_path: Path) -> None:
    """A super-admin override can manage a community they do not own."""
    database = build_database(tmp_path, "ban-user-super-admin.db")
    community = _local_community(database, owner_id="111")

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=["999"]),
            discord_user_id="999",
            community_slug="cats",
            actor_handle="alice@example.com",
            reason="spam",
        )
    )
    ban = database.community_actor_bans.get_active_ban_by_handle(
        local_community_id=community.id,
        actor_handle="alice@example.com",
    )

    assert result.applied is True
    assert ban is not None
    assert ban.created_by_discord_user_id == "999"


def test_non_owner_non_admin_cannot_ban_in_owned_community(tmp_path: Path) -> None:
    """An unrelated user is rejected and no moderation row is created."""
    database = build_database(tmp_path, "ban-user-denied.db")
    _local_community(database, owner_id="111")

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=[]),
            discord_user_id="222",
            community_slug="cats",
            actor_handle="alice@example.com",
            reason="spam",
        )
    )

    assert result.applied is False
    assert result.reason == "cannot_manage_community"
    assert result.message == "You are not allowed to manage this local community."
    assert _ban_count(database) == 0


def test_owner_can_manage_without_super_admin_status(tmp_path: Path) -> None:
    """Ownership must not silently regress back to allowlist-only moderation."""
    database = build_database(tmp_path, "ban-user-owner-not-admin.db")
    _local_community(database, owner_id="111")

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=["999"]),
            discord_user_id="111",
            community_slug="cats",
            actor_handle="alice@example.com",
            reason=None,
        )
    )

    assert result.applied is True
    assert result.reason == "created"
    assert _ban_count(database) == 1


def test_super_admin_can_manage_without_being_owner(tmp_path: Path) -> None:
    """The explicit super-admin override must remain available."""
    database = build_database(tmp_path, "ban-user-admin-not-owner.db")
    _local_community(database, owner_id="111")

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=["999"]),
            discord_user_id="999",
            community_slug="cats",
            actor_handle="alice@example.com",
            reason=None,
        )
    )

    assert result.applied is True
    assert result.reason == "created"


def test_legacy_null_owned_community_can_be_managed_by_super_admin(tmp_path: Path) -> None:
    """Legacy NULL-owned communities remain manageable by super-admins only."""
    database = build_database(tmp_path, "ban-user-legacy-admin.db")
    _local_community(database, owner_id=None)

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=["999"]),
            discord_user_id="999",
            community_slug="cats",
            actor_handle="alice@example.com",
            reason=None,
        )
    )

    assert result.applied is True
    assert _ban_count(database) == 1


def test_legacy_null_owned_community_rejects_ordinary_user(tmp_path: Path) -> None:
    """A NULL owner must not accidentally mean that everyone can manage it."""
    database = build_database(tmp_path, "ban-user-legacy-user.db")
    _local_community(database, owner_id=None)

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=[]),
            discord_user_id="111",
            community_slug="cats",
            actor_handle="alice@example.com",
            reason=None,
        )
    )

    assert result.applied is False
    assert result.reason == "cannot_manage_community"
    assert _ban_count(database) == 0


def test_unknown_community_slug_is_rejected_before_other_checks(tmp_path: Path) -> None:
    """A missing community slug must not create a global or orphan ban."""
    database = build_database(tmp_path, "ban-user-unknown.db")

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=["123"]),
            discord_user_id="123",
            community_slug="missing",
            actor_handle="not-a-handle",
            reason=None,
        )
    )

    assert result.applied is False
    assert result.reason == "unknown_community"
    assert result.message == "Unknown local community slug: missing"
    assert _ban_count(database) == 0


def test_unauthorized_caller_is_rejected_before_invalid_handle_validation(tmp_path: Path) -> None:
    """Authorization failure must not leak handle-validation feedback."""
    database = build_database(tmp_path, "ban-user-denied-invalid.db")
    _local_community(database, owner_id="111")

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=[]),
            discord_user_id="222",
            community_slug="cats",
            actor_handle="https://example.com/u/alice",
            reason=None,
        )
    )

    assert result.applied is False
    assert result.reason == "cannot_manage_community"
    assert result.message == "You are not allowed to manage this local community."
    assert _ban_count(database) == 0


def test_authorized_caller_with_invalid_handle_is_rejected(tmp_path: Path) -> None:
    """Authorized callers still need the v1 user@example.com handle shape."""
    database = build_database(tmp_path, "ban-user-invalid.db")
    _local_community(database, owner_id="111")

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=[]),
            discord_user_id="111",
            community_slug="cats",
            actor_handle="https://example.com/u/alice",
            reason=None,
        )
    )

    assert result.applied is False
    assert result.reason == "invalid_handle"
    assert result.message == "Invalid remote user handle. Use user@example.com."
    assert _ban_count(database) == 0


def test_authorized_duplicate_active_ban_reports_existing_reason(tmp_path: Path) -> None:
    """Duplicate active bans are rejected without editing the old reason."""
    database = build_database(tmp_path, "ban-user-duplicate.db")
    community = _local_community(database, owner_id="111")
    database.community_actor_bans.create_active_ban(
        local_community_id=community.id,
        actor_handle="alice@example.com",
        actor_url=None,
        created_by_discord_user_id="111",
        reason="spam",
    )

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=[]),
            discord_user_id="111",
            community_slug="cats",
            actor_handle="alice@example.com",
            reason="new reason",
        )
    )
    existing = database.community_actor_bans.get_active_ban_by_handle(
        local_community_id=community.id,
        actor_handle="alice@example.com",
    )

    assert result.applied is False
    assert result.reason == "duplicate_active_ban"
    assert result.message == "User alice@example.com is already banned in community cats.\nReason: spam"
    assert _ban_count(database) == 1
    assert existing.reason == "spam"
    assert existing.created_by_discord_user_id == "111"


def test_duplicate_active_ban_without_reason_reports_not_specified(tmp_path: Path) -> None:
    """Duplicate rejection text stays explicit when the stored reason is empty."""
    database = build_database(tmp_path, "ban-user-duplicate-empty.db")
    community = _local_community(database, owner_id="111")
    database.community_actor_bans.create_active_ban(
        local_community_id=community.id,
        actor_handle="alice@example.com",
        actor_url=None,
        created_by_discord_user_id="111",
        reason=None,
    )

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=[]),
            discord_user_id="111",
            community_slug="cats",
            actor_handle="alice@example.com",
            reason="new reason",
        )
    )

    assert result.applied is False
    assert result.message == "User alice@example.com is already banned in community cats.\nReason: not specified"
    assert _ban_count(database) == 1


def test_discord_user_id_comparison_is_string_exact(tmp_path: Path) -> None:
    """Discord owner ids must not be coerced into integers for comparison."""
    database = build_database(tmp_path, "ban-user-string-exact.db")
    _local_community(database, owner_id="123")

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=[]),
            discord_user_id="0123",
            community_slug="cats",
            actor_handle="alice@example.com",
            reason=None,
        )
    )

    assert result.applied is False
    assert result.reason == "cannot_manage_community"
    assert _ban_count(database) == 0


def test_ban_user_reactivates_inactive_row_after_unban(tmp_path: Path) -> None:
    """Repeated ban/unban reuses the inactive row under current uniqueness."""
    database = build_database(tmp_path, "ban-user-reactivate.db")
    community = _local_community(database, owner_id="111")
    original = database.community_actor_bans.create_active_ban(
        local_community_id=community.id,
        actor_handle="alice@example.com",
        actor_url=None,
        created_by_discord_user_id="111",
        reason="old reason",
    )
    database.community_actor_bans.deactivate_active_ban_by_handle(
        local_community_id=community.id,
        actor_handle="alice@example.com",
    )
    inactive = database.community_actor_bans.get_inactive_ban_by_handle(
        local_community_id=community.id,
        actor_handle="alice@example.com",
    )

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=[]),
            discord_user_id="222",
            community_slug="cats",
            actor_handle="alice@example.com",
            reason="new reason",
        )
    )
    active = database.community_actor_bans.get_active_ban_by_handle(
        local_community_id=community.id,
        actor_handle="alice@example.com",
    )

    assert result.applied is False
    assert result.reason == "cannot_manage_community"
    assert active is None
    assert inactive is not None
    assert inactive.id == original.id


def test_owner_reban_reactivates_inactive_row_and_updates_active_fields(tmp_path: Path) -> None:
    """Owner re-ban after unban reuses row and updates current active metadata."""
    database = build_database(tmp_path, "ban-user-owner-reactivate.db")
    community = _local_community(database, owner_id="111")
    original = database.community_actor_bans.create_active_ban(
        local_community_id=community.id,
        actor_handle="alice@example.com",
        actor_url=None,
        created_by_discord_user_id="999",
        reason="old reason",
    )
    original_created_at = original.created_at
    database.community_actor_bans.deactivate_active_ban_by_handle(
        local_community_id=community.id,
        actor_handle="alice@example.com",
    )

    result = ban_user_operation(
        BanUserInput(
            database=database,
            settings=_settings(super_admins=[]),
            discord_user_id="111",
            community_slug="cats",
            actor_handle="alice@example.com",
            reason="new reason",
        )
    )
    active = database.community_actor_bans.get_active_ban_by_handle(
        local_community_id=community.id,
        actor_handle="alice@example.com",
    )

    assert result.applied is True
    assert active is not None
    assert active.id == original.id
    assert active.status == "active"
    assert active.reason == "new reason"
    assert active.created_by_discord_user_id == "111"
    assert active.created_at.replace(tzinfo=None) == original_created_at.replace(tzinfo=None)
    assert _ban_count(database) == 1
