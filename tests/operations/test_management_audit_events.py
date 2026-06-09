"""Observable behavior tests for backend management audit events."""

from __future__ import annotations
from src.bridge_policy import BridgePolicyService

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from src.db import Database
from src.local_communities.service import LocalCommunityService
from src.management_audit import (
    ACTION_BAN_CREATE_FORBIDDEN,
    ACTION_BAN_CREATED,
    ACTION_BAN_REACTIVATED,
    ACTION_BAN_REMOVED,
    ACTION_BAN_REMOVE_FORBIDDEN,
    ACTION_COMMUNITY_CREATE_FORBIDDEN,
    ACTION_COMMUNITY_CREATED,
    ACTION_COMMUNITY_MANAGE_FORBIDDEN,
    ACTION_COMMUNITY_METADATA_UPDATED,
    ACTION_COMMUNITY_STATUS_CHANGED,
    REASON_COMMUNITY_DISABLED,
    REASON_NOT_OWNER_OR_SUPER_ADMIN,
    REASON_NOT_SUPER_ADMIN,
    RESULT_FORBIDDEN,
    RESULT_SUCCESS,
    TARGET_LOCAL_COMMUNITY,
)
from src.models import CommunityActorBan, ManagementAuditEvent
from src.operations import (
    BanUserInput,
    CreateCommunityInput,
    EditCommunityInput,
    ListBannedUsersInput,
    UnbanUserInput,
    ban_user_operation,
    create_community_operation,
    edit_community_operation,
    list_banned_users_operation,
    unban_user_operation,
)
from support.db import build_database


def _settings(*, super_admins: list[str] | None = None) -> SimpleNamespace:
    """Build the settings fields read by management operations."""
    return SimpleNamespace(
        bridge_super_admin_user_ids=super_admins or [],
        normalized_fedify_origin="https://bridge.example",
    )


def _community(database: Database, *, slug: str = "cats", owner_id: str = "111", status: str = "active") -> object:
    """Create one local community and return its persisted row."""
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug=slug,
        name="Cats",
        description="Old summary",
        created_by_discord_user_id=owner_id,
    )
    community = database.local_communities.get_local_community_by_slug(slug)
    if status != "active":
        # Tests model lifecycle state as a precondition; the creation path itself
        # is always active and separately audited.
        with database.session() as session:
            persisted = session.merge(community)
            persisted.status = status
    return database.local_communities.get_local_community_by_slug(slug)


def _audit_rows(database: Database) -> list[ManagementAuditEvent]:
    """Return audit rows in insertion order for direct assertions."""
    return database.management_audit_events.list_oldest_first()


def _rows_after_setup(database: Database) -> list[ManagementAuditEvent]:
    """Return rows after the setup-only community creation event."""
    return _audit_rows(database)[1:]


def test_clean_database_and_migration_create_management_audit_table(tmp_path: Path) -> None:
    """Clean schema and migration-only paths both create the audit table."""
    clean = build_database(tmp_path, "audit-clean.db")
    existing = build_database(tmp_path, "audit-existing.db")
    with existing.session() as session:
        session.execute(text("DROP TABLE management_audit_events"))
    existing.migrate()

    with clean.session() as session:
        clean_tables = {row[0] for row in session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    with existing.session() as session:
        migrated_tables = {row[0] for row in session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}

    assert "management_audit_events" in clean_tables
    assert "management_audit_events" in migrated_tables


def test_repository_stores_canonical_json_and_rejects_invalid_vocabulary(tmp_path: Path) -> None:
    """Repository creation stores compact sorted JSON and validates v1 values."""
    database = build_database(tmp_path, "audit-repository.db")

    database.management_audit_events.create_event(
        action=ACTION_COMMUNITY_METADATA_UPDATED,
        result=RESULT_SUCCESS,
        actor_discord_user_id="111",
        target_type=TARGET_LOCAL_COMMUNITY,
        local_community_id=1,
        target_id="1",
        before={"summary": "old", "display_name": "Old"},
        after={"summary": "new", "display_name": "New"},
    )
    row = _audit_rows(database)[0]

    assert row.before_json == '{"display_name":"Old","summary":"old"}'
    assert row.after_json == '{"display_name":"New","summary":"new"}'
    with pytest.raises(ValueError):
        database.management_audit_events.create_event(
            action="audit.created",
            result=RESULT_SUCCESS,
            actor_discord_user_id="111",
            target_type=TARGET_LOCAL_COMMUNITY,
        )


def test_create_community_success_is_audited_for_registered_user_flow(tmp_path: Path) -> None:
    """Community creation now writes only safe success rows at operation level."""
    database = build_database(tmp_path, "audit-create-community.db")

    created = create_community_operation(
        CreateCommunityInput(
            database=database,
            settings=_settings(super_admins=[]),
            discord_user_id="111",
            discord_guild_id=10,
            discord_forum_channel_id=100,
            slug="hackers",
            name="Hackers",
            description="A local forum.",
        )
    )
    rows = _audit_rows(database)
    after = json.loads(rows[0].after_json or "{}")

    assert created.applied is True
    assert [row.action for row in rows] == [ACTION_COMMUNITY_CREATED]
    assert rows[0].result == RESULT_SUCCESS
    assert rows[0].local_community_id is not None
    assert rows[0].target_id == str(rows[0].local_community_id)
    assert after["slug"] == "hackers"
    assert after["created_by_discord_user_id"] == "111"
    assert "private_key_pem" not in after
    assert "public_key_pem" not in after


def test_edit_community_audits_metadata_status_forbidden_and_skips_noop(tmp_path: Path) -> None:
    """Community edits audit changed fields only and skip no-op saves."""
    database = build_database(tmp_path, "audit-edit-community.db")
    community = _community(database)

    no_op = edit_community_operation(
        EditCommunityInput(
            database=database,
            settings=_settings(),
            discord_user_id="111",
            discord_guild_id=10,
            community_slug="cats",
            display_name="Cats",
            summary="Old summary",
            status="active",

            policy_service=BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries),)
    )
    updated = edit_community_operation(
        EditCommunityInput(
            database=database,
            settings=_settings(),
            discord_user_id="111",
            discord_guild_id=10,
            community_slug="cats",
            display_name="New Cats",
            summary=None,
            status="disabled",

            policy_service=BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries),)
    )
    forbidden = edit_community_operation(
        EditCommunityInput(
            database=database,
            settings=_settings(),
            discord_user_id="222",
            discord_guild_id=10,
            community_slug="cats",
            display_name="Bad Cats",
            summary=None,
            status="disabled",

            policy_service=BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries),)
    )
    rows = _rows_after_setup(database)

    assert no_op.applied is True
    assert updated.applied is True
    assert forbidden.reason == "cannot_manage_community"
    assert [row.action for row in rows] == [
        ACTION_COMMUNITY_METADATA_UPDATED,
        ACTION_COMMUNITY_STATUS_CHANGED,
        ACTION_COMMUNITY_MANAGE_FORBIDDEN,
    ]
    assert rows[0].local_community_id == community.id
    assert rows[0].before_json == '{"display_name":"Cats","summary":"Old summary"}'
    assert rows[0].after_json == '{"display_name":"New Cats","summary":null}'
    assert rows[1].before_json == '{"status":"active"}'
    assert rows[1].after_json == '{"status":"disabled"}'
    assert rows[2].reason_code == REASON_NOT_OWNER_OR_SUPER_ADMIN
    assert rows[2].before_json is None
    assert rows[2].after_json is None


def test_ban_and_unban_success_forbidden_and_validation_audit_boundaries(tmp_path: Path) -> None:
    """Ban/unban operations audit success and selected denials only."""
    database = build_database(tmp_path, "audit-ban-unban.db")
    community = _community(database)

    invalid = ban_user_operation(
        BanUserInput(database, _settings(), "111", 10, "cats", "not a handle", BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries), None)
    )
    created = ban_user_operation(
        BanUserInput(database, _settings(), "111", 10, "cats", "alice@example.com", BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries), "spam")
    )
    duplicate = ban_user_operation(
        BanUserInput(database, _settings(), "111", 10, "cats", "alice@example.com", BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries), None)
    )
    removed = unban_user_operation(
        UnbanUserInput(database, _settings(), "111", 10, "cats", "alice@example.com", BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries))
    )
    reactivated = ban_user_operation(
        BanUserInput(database, _settings(), "111", 10, "cats", "alice@example.com", BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries), "again")
    )
    forbidden = ban_user_operation(
        BanUserInput(database, _settings(), "222", 10, "cats", "bob@example.com", BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries), None)
    )
    rows = _rows_after_setup(database)

    assert invalid.reason == "invalid_handle"
    assert created.applied is True
    assert duplicate.reason == "duplicate_active_ban"
    assert removed.applied is True
    assert reactivated.applied is True
    assert forbidden.reason == "cannot_manage_community"
    assert [row.action for row in rows] == [
        ACTION_BAN_CREATED,
        ACTION_BAN_REMOVED,
        ACTION_BAN_REACTIVATED,
        ACTION_BAN_CREATE_FORBIDDEN,
    ]
    assert rows[0].local_community_id == community.id
    assert rows[0].target_id == "alice@example.com"
    assert json.loads(rows[0].after_json or "{}")["status"] == "active"
    assert rows[1].before_json == '{"status":"active"}'
    assert rows[1].after_json == '{"status":"inactive"}'
    assert json.loads(rows[2].before_json or "{}")["status"] == "inactive"
    assert json.loads(rows[2].after_json or "{}")["reason"] == "again"
    assert rows[3].result == RESULT_FORBIDDEN
    assert rows[3].reason_code == REASON_NOT_OWNER_OR_SUPER_ADMIN


def test_disabled_community_denials_are_audited_but_read_only_list_is_not(tmp_path: Path) -> None:
    """Disabled moderation denials are audited while list command stays quiet."""
    database = build_database(tmp_path, "audit-disabled.db")
    community = _community(database, status="disabled")
    with database.session() as session:
        session.add(
            CommunityActorBan(
                local_community_id=community.id,
                actor_handle="alice@example.com",
                actor_url=None,
                status="active",
                created_by_discord_user_id="111",
                reason="spam",
            )
        )

    ban_result = ban_user_operation(
        BanUserInput(database, _settings(), "111", 10, "cats", "bob@example.com", BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries), None)
    )
    unban_result = unban_user_operation(
        UnbanUserInput(database, _settings(), "111", 10, "cats", "alice@example.com", BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries))
    )
    list_result = list_banned_users_operation(
        ListBannedUsersInput(database, _settings(), "111", 10, "cats", BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries))
    )
    rows = _rows_after_setup(database)

    assert ban_result.reason == "community_disabled"
    assert unban_result.reason == "community_disabled"
    assert list_result.reason == "community_disabled"
    assert [row.action for row in rows] == [ACTION_BAN_CREATE_FORBIDDEN, ACTION_BAN_REMOVE_FORBIDDEN]
    assert [row.reason_code for row in rows] == [REASON_COMMUNITY_DISABLED, REASON_COMMUNITY_DISABLED]


def test_validation_and_guild_context_failures_do_not_create_audit_rows(tmp_path: Path) -> None:
    """Validation/not-found/context failures remain outside v1 audit scope."""
    database = build_database(tmp_path, "audit-validation-boundaries.db")
    _community(database)

    bad_create = create_community_operation(
        CreateCommunityInput(
            database=database,
            settings=_settings(super_admins=["111"]),
            discord_user_id="111",
            discord_guild_id=10,
            discord_forum_channel_id=200,
            slug="Invalid Slug",
            name="Bad",
            description=None,
        )
    )
    unknown_edit = edit_community_operation(
        EditCommunityInput(database, _settings(), "111", 10, "missing", "Name", None, BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries), "active")
    )
    guildless_ban = ban_user_operation(
        BanUserInput(database, _settings(super_admins=["111"]), "111", None, "cats", "alice@example.com", BridgePolicyService(settings=_settings(super_admins=["111"]), repository=database.bridge_policy_entries), None)
    )
    no_active_unban = unban_user_operation(
        UnbanUserInput(database, _settings(), "111", 10, "cats", "alice@example.com", BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries))
    )
    rows = _rows_after_setup(database)

    assert bad_create.reason == "validation_failed"
    assert unknown_edit.reason == "unknown_or_inaccessible_community"
    assert guildless_ban.reason == "missing_guild_context"
    assert no_active_unban.reason == "no_active_ban"
    assert rows == []


def test_successful_create_rolls_back_when_audit_insert_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Creation and success audit share a transaction boundary."""
    database = build_database(tmp_path, "audit-create-rollback.db")

    def fail_add_event(*_: object, **__: object) -> None:
        """Simulate an audit insert failure from inside the transaction."""
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(database.management_audit_events, "add_event", fail_add_event)

    with pytest.raises(RuntimeError, match="audit insert failed"):
        create_community_operation(
            CreateCommunityInput(
                database=database,
                settings=_settings(super_admins=["111"]),
                discord_user_id="111",
                discord_guild_id=10,
                discord_forum_channel_id=100,
                slug="cats",
                name="Cats",
                description=None,
            )
        )

    assert database.local_communities.get_local_community_by_slug("cats") is None


def test_successful_edit_and_ban_roll_back_when_audit_insert_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful state mutations do not commit without their audit rows."""
    database = build_database(tmp_path, "audit-mutation-rollback.db")
    community = _community(database)

    def fail_add_event(*_: object, **__: object) -> None:
        """Simulate an audit insert failure from inside mutation transactions."""
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(database.management_audit_events, "add_event", fail_add_event)

    with pytest.raises(RuntimeError, match="audit insert failed"):
        edit_community_operation(
            EditCommunityInput(database, _settings(), "111", 10, "cats", "New Cats", None, BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries), "disabled")
        )
    unchanged = database.local_communities.get_local_community_by_slug("cats")

    with pytest.raises(RuntimeError, match="audit insert failed"):
        ban_user_operation(
            BanUserInput(database, _settings(), "111", 10, "cats", "alice@example.com", BridgePolicyService(settings=_settings(), repository=database.bridge_policy_entries), "spam")
        )
    ban = database.community_actor_bans.get_active_ban_by_handle(
        local_community_id=community.id,
        actor_handle="alice@example.com",
    )

    assert unchanged.display_name == "Cats"
    assert unchanged.summary == "Old summary"
    assert unchanged.status == "active"
    assert ban is None
