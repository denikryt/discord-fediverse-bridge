"""Plan 93 behavior coverage for generalized local and global user bans."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.content_publish_service import ContentPublishService
from src.db import Database
from src.fedify_gateway_client import PublishContentResult
from src.user_bans import UnknownLocalBanTarget, UserBanService, resolve_ban_target


def _settings() -> SimpleNamespace:
    """Return the identity fields needed by ban policy and canonical labels."""
    return SimpleNamespace(normalized_public_base_url="https://bridge.example:8443")


def _database(tmp_path: Path) -> Database:
    """Create one real current-schema SQLite database."""
    database = Database(f"sqlite:///{tmp_path / 'plan93.db'}")
    database.create_all()
    return database


def _user(database: Database) -> object:
    """Seed one registered local Discord/ActivityPub identity."""
    return database.users.create_user(
        discord_user_id="123", activitypub_username="Alice",
        actor_url="https://bridge.example:8443/users/Alice",
        inbox_url="https://bridge.example:8443/users/Alice/inbox",
        outbox_url="https://bridge.example:8443/users/Alice/outbox",
        followers_url="https://bridge.example:8443/users/Alice/followers",
        public_key_pem="public", private_key_pem="private",
    )


def test_local_target_resolution_preserves_username_and_normalizes_authority(tmp_path: Path) -> None:
    """A configured local authority resolves from users instead of becoming remote."""
    database = _database(tmp_path)
    _user(database)

    target = resolve_ban_target(database=database, settings=_settings(), value="Alice@BRIDGE.EXAMPLE:8443")

    assert target.actor_handle == "Alice@bridge.example:8443"
    assert target.discord_user_id == "123"
    assert target.is_local is True


def test_unknown_local_target_is_validation_failure(tmp_path: Path) -> None:
    """An unknown username on the bridge authority cannot be stored as remote."""
    database = _database(tmp_path)
    with pytest.raises(UnknownLocalBanTarget):
        resolve_ban_target(database=database, settings=_settings(), value="missing@bridge.example:8443")


def test_global_discord_ban_matches_immutable_discord_id(tmp_path: Path) -> None:
    """Global command/publish policy attaches to Discord id, not mutable nickname."""
    database = _database(tmp_path)
    user = _user(database)
    with database.session() as session:
        database.community_actor_bans.create_or_reactivate_active_ban(
            session, local_community_id=None, actor_handle="Alice@bridge.example:8443",
            actor_url=user.actor_url, target_discord_user_id="123",
            created_by_discord_user_id="999", reason="abuse",
        )

    decision = UserBanService(database=database, settings=_settings()).check_global_discord_user("123")

    assert decision.banned is True
    assert decision.scope == "global"
    assert decision.reason == "abuse"


@pytest.mark.asyncio
async def test_global_ban_rejects_remote_subscription_publish_before_side_effects(tmp_path: Path) -> None:
    """A globally banned registered sender gets one source reply and no gateway call."""
    database = _database(tmp_path)
    user = _user(database)
    database.remote_subscriptions.create_subscription(
        discord_channel_id=100,
        lemmy_community_actor_id="https://remote.example/c/cats",
        lemmy_community_name="cats", lemmy_community_id=1,
        community_handle="!cats@remote.example",
        community_inbox_url="https://remote.example/c/cats/inbox",
        follow_activity_id="https://bridge.example/follows/1", status="accepted",
    )
    with database.session() as session:
        database.community_actor_bans.create_or_reactivate_active_ban(
            session, local_community_id=None, actor_handle="Alice@bridge.example:8443",
            actor_url=user.actor_url, target_discord_user_id="123",
            created_by_discord_user_id="999", reason="abuse",
        )
    gateway = AsyncMock()
    gateway.publish_content.return_value = PublishContentResult(activity_id="a", object_id="o", community_actor_url="https://remote.example/c/cats")
    service = ContentPublishService(database=database, fedify_gateway=gateway, bridge_prefix="[bridge]", settings=_settings())
    message = SimpleNamespace(
        id=300, content="blocked", author=SimpleNamespace(id=123, display_name="Changed"), reply=AsyncMock()
    )

    result = await service.publish_thread_starter(
        thread=SimpleNamespace(id=200, parent_id=100, name="Blocked"), starter_message=message
    )

    assert result.status == "rejected"
    assert result.reason == "user_banned"
    message.reply.assert_awaited_once_with("You were banned from this bridge instance.\nReason: abuse")
    gateway.publish_content.assert_not_awaited()


def test_legacy_community_bans_migrate_without_losing_state(tmp_path: Path) -> None:
    """Legacy community-only rows become explicit community scope rows."""
    from sqlalchemy import create_engine, text

    path = tmp_path / "legacy-plan93.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE community_actor_bans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_community_id INTEGER NOT NULL,
                actor_handle VARCHAR(255) NOT NULL,
                actor_url VARCHAR(512),
                status VARCHAR(32) NOT NULL,
                created_by_discord_user_id VARCHAR(64),
                reason VARCHAR(1024),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE (local_community_id, actor_handle, status)
            )
        """))
        connection.execute(text("""
            INSERT INTO community_actor_bans (
                local_community_id, actor_handle, actor_url, status,
                created_by_discord_user_id, reason, created_at, updated_at
            ) VALUES (7, 'bob@remote.example', NULL, 'inactive', '999', 'old reason',
                      '2026-01-01 00:00:00', '2026-02-01 00:00:00')
        """))
    database = Database(f"sqlite:///{path}")
    from src.db.migrations import _migrate_generalized_user_bans
    with database.engine.begin() as connection:
        _migrate_generalized_user_bans(connection)

    ban = database.community_actor_bans.get_inactive_ban_by_handle(
        local_community_id=7, actor_handle="bob@remote.example"
    )
    assert ban is not None
    assert ban.scope == "community"
    assert ban.scope_key == "7"
    assert ban.reason == "old reason"
    assert ban.created_by_discord_user_id == "999"


@pytest.mark.asyncio
async def test_command_tree_rejects_global_ban_ephemerally(tmp_path: Path) -> None:
    """The centralized command-tree boundary rejects before command callbacks."""
    from src.command_tree import BridgeCommandTree

    database = _database(tmp_path)
    user = _user(database)
    with database.session() as session:
        database.community_actor_bans.create_or_reactivate_active_ban(
            session, local_community_id=None, actor_handle="Alice@bridge.example:8443",
            actor_url=user.actor_url, target_discord_user_id="123",
            created_by_discord_user_id="999", reason="abuse",
        )
    tree = object.__new__(BridgeCommandTree)
    tree.ban_service = UserBanService(database=database, settings=_settings())
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=123),
        response=SimpleNamespace(send_message=AsyncMock()),
    )

    allowed = await tree.interaction_check(interaction)

    assert allowed is False
    interaction.response.send_message.assert_awaited_once_with(
        "You were banned from this bridge instance.\nReason: abuse", ephemeral=True
    )


def test_scoped_ban_does_not_trigger_global_command_lockout(tmp_path: Path) -> None:
    """Community policy remains separate from bridge-wide command access."""
    database = _database(tmp_path)
    user = _user(database)
    with database.session() as session:
        database.community_actor_bans.create_or_reactivate_active_ban(
            session, local_community_id=7, actor_handle="Alice@bridge.example:8443",
            actor_url=user.actor_url, target_discord_user_id="123",
            created_by_discord_user_id="999", reason="community-only",
        )

    decision = UserBanService(database=database, settings=_settings()).check_global_discord_user("123")

    assert decision.banned is False


def test_discord_fanout_header_uses_registered_local_handle(tmp_path: Path) -> None:
    """Discord mirror attribution is independent from mutable guild nicknames."""
    from src.community_sync.discord_fanout import DiscordFanout

    database = _database(tmp_path)
    _user(database)
    fanout = DiscordFanout(bot=SimpleNamespace(database=database, settings=_settings()))
    message = SimpleNamespace(author=SimpleNamespace(id=123, display_name="Different Nick"))

    assert fanout._author_label(message) == "Alice@bridge.example:8443"
