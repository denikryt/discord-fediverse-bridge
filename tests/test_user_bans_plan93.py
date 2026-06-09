"""Runtime scenarios for local and global bridge user bans.

Each test drives a user-visible command, publish action, migration startup, or
Discord fanout boundary and then asserts persisted or delivered outcomes.
"""

from __future__ import annotations
from support.runtime import build_test_policy_service

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, select, text

from src.commands import ban_user
from src.community_sync.discord_fanout import DiscordFanout
from src.content_publish_service import ContentPublishService
from src.db import Database
from src.local_communities.service import LocalCommunityService
from src.models import CommunityActorBan, ManagementAuditEvent
from src.operations import UnbanUserInput, unban_user_operation
from support.db import add_accepted_subscription, build_database, create_source_thread_group
from support.discord import build_forum_channel_object_result, build_starter_message, build_thread, build_thread_message
from support.gateway import build_publish_result


@dataclass
class _RegisteredCommand:
    """Store one decorated command callback for scenario execution."""

    callback: object


class _RecordingTree:
    """Capture command callbacks without opening a Discord connection."""

    def __init__(self) -> None:
        self.commands: dict[str, _RegisteredCommand] = {}

    def command(self, *, name: str, description: str):
        """Record the decorated callback under its Discord command name."""

        def decorator(callback: object) -> object:
            self.commands[name] = _RegisteredCommand(callback=callback)
            return callback

        return decorator


def _settings(*, super_admins: list[str] | None = None) -> SimpleNamespace:
    """Build settings used by command access, identity, and ban rendering."""
    return SimpleNamespace(
        discord_guild_allowlist=[],
        bridge_super_admin_user_ids=super_admins or [],
        normalized_public_base_url="https://bridge.example:8443",
    )


def _registered_local_user(database: Database, *, discord_user_id: str = "123", username: str = "Alice") -> object:
    """Create one local bridge identity used by moderation scenarios."""
    actor_url = f"https://bridge.example:8443/users/{username}"
    return database.users.create_user(
        discord_user_id=discord_user_id,
        activitypub_username=username,
        actor_url=actor_url,
        inbox_url=f"{actor_url}/inbox",
        outbox_url=f"{actor_url}/outbox",
        followers_url=f"{actor_url}/followers",
        public_key_pem="public",
        private_key_pem="private",
    )


def _local_community(database: Database, *, slug: str = "cats", owner_id: str = "111", guild_id: int = 10) -> object:
    """Create one active local community through the production service."""
    LocalCommunityService(
        database=database,
        base_url="https://bridge.example:8443",
        keypair_generator=lambda: ("public", "private"),
    ).create_local_community(
        discord_guild_id=guild_id,
        discord_forum_channel_id=100 if slug == "cats" else 101,
        slug=slug,
        name=slug.title(),
        description=f"{slug} community",
        created_by_discord_user_id=owner_id,
    )
    return database.local_communities.get_local_community_by_slug(slug)


def _interaction(*, caller_id: str, guild_id: int | None = 10, dm_user: object | None = None) -> SimpleNamespace:
    """Build one Discord interaction with observable response and DM surfaces."""
    client = SimpleNamespace(fetch_user=AsyncMock(return_value=dm_user))
    return SimpleNamespace(
        user=SimpleNamespace(id=caller_id, guild_permissions=SimpleNamespace(manage_guild=True)),
        guild_id=guild_id,
        response=SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
        client=client,
    )


def _active_bans(database: Database) -> list[CommunityActorBan]:
    """Return all active rows so command scenarios can assert exact mutations."""
    with database.session() as session:
        return list(session.scalars(select(CommunityActorBan).where(CommunityActorBan.status == "active")))


def _audit_actions(database: Database) -> list[str]:
    """Return persisted audit action names in insertion order."""
    with database.session() as session:
        rows = session.scalars(select(ManagementAuditEvent).order_by(ManagementAuditEvent.id)).all()
        return [row.action for row in rows if row.action.startswith("ban.")]


@pytest.mark.asyncio
async def test_owner_command_bans_local_user_and_delivers_private_community_notice(tmp_path: Path) -> None:
    """An owner action persists one scoped ban/audit and privately notifies the target."""
    database = build_database(tmp_path, "plan93-command-community.db")
    community = _local_community(database)
    target = _registered_local_user(database)
    dm_user = SimpleNamespace(send=AsyncMock())
    interaction = _interaction(caller_id="111", dm_user=dm_user)
    tree = _RecordingTree()
    ban_user.register(tree, database, _settings(), policy_service=build_test_policy_service(database, _settings(super_admins=["999"])))

    await tree.commands["ban-user"].callback(
        interaction,
        user="Alice@BRIDGE.EXAMPLE:8443",
        community="cats",
        reason="spam",
    )

    bans = _active_bans(database)
    assert len(bans) == 1
    assert bans[0].local_community_id == community.id
    assert bans[0].actor_handle == "Alice@bridge.example:8443"
    assert bans[0].target_discord_user_id == target.discord_user_id
    assert _audit_actions(database) == ["ban.created"]
    interaction.response.send_message.assert_awaited_once_with(
        "Banned Alice@bridge.example:8443 from community cats.\nReason: spam",
        ephemeral=True,
    )
    dm_user.send.assert_awaited_once_with(
        "You were banned from community cats@bridge.example:8443.\nReason: spam"
    )


@pytest.mark.asyncio
async def test_super_admin_command_bans_local_user_globally_and_delivers_private_notice(tmp_path: Path) -> None:
    """A global command creates one bridge-wide row and the distinct global DM."""
    database = build_database(tmp_path, "plan93-command-global.db")
    _registered_local_user(database)
    dm_user = SimpleNamespace(send=AsyncMock())
    interaction = _interaction(caller_id="999", dm_user=dm_user)
    tree = _RecordingTree()
    ban_user.register(
        tree,
        database,
        _settings(super_admins=["999"]),
        policy_service=build_test_policy_service(database, _settings(super_admins=["999"])),
    )

    await tree.commands["ban-user"].callback(
        interaction,
        user="Alice@bridge.example:8443",
        community=None,
        reason="abuse",
    )

    bans = _active_bans(database)
    assert len(bans) == 1
    assert bans[0].local_community_id is None
    assert bans[0].scope == "global"
    assert _audit_actions(database) == ["ban.created"]
    dm_user.send.assert_awaited_once_with(
        "You were banned from this bridge instance.\nReason: abuse"
    )


@pytest.mark.asyncio
async def test_owner_global_ban_attempt_stops_before_target_lookup_or_mutation(tmp_path: Path) -> None:
    """Authorization denial does not disclose target existence or create side effects."""
    database = build_database(tmp_path, "plan93-command-denied.db")
    interaction = _interaction(caller_id="111")
    tree = _RecordingTree()
    ban_user.register(tree, database, _settings(), policy_service=build_test_policy_service(database, _settings()))

    await tree.commands["ban-user"].callback(
        interaction,
        user="missing@bridge.example:8443",
        community=None,
        reason="test",
    )

    assert _active_bans(database) == []
    assert _audit_actions(database) == ["ban.create_forbidden"]
    interaction.response.send_message.assert_awaited_once_with(
        "Only a super-admin can create a global ban.",
        ephemeral=True,
    )
    interaction.client.fetch_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_active_ban_does_not_create_row_audit_or_second_dm(tmp_path: Path) -> None:
    """Repeating the same command reports existing state without new effects."""
    database = build_database(tmp_path, "plan93-command-duplicate.db")
    _local_community(database)
    _registered_local_user(database)
    dm_user = SimpleNamespace(send=AsyncMock())
    interaction = _interaction(caller_id="111", dm_user=dm_user)
    tree = _RecordingTree()
    ban_user.register(tree, database, _settings(), policy_service=build_test_policy_service(database, _settings()))

    callback = tree.commands["ban-user"].callback
    await callback(interaction, user="Alice@bridge.example:8443", community="cats", reason="spam")
    await callback(interaction, user="Alice@bridge.example:8443", community="cats", reason="again")

    assert len(_active_bans(database)) == 1
    assert _audit_actions(database) == ["ban.created"]
    assert dm_user.send.await_count == 1
    assert interaction.response.send_message.await_args_list[-1].args[0] == (
        "User Alice@bridge.example:8443 is already banned in community cats.\nReason: spam"
    )


@pytest.mark.asyncio
async def test_reactivation_reuses_row_updates_reason_and_sends_one_new_dm(tmp_path: Path) -> None:
    """Unban followed by ban reactivates the same row and emits reactivation effects."""
    database = build_database(tmp_path, "plan93-command-reactivate.db")
    community = _local_community(database)
    _registered_local_user(database)
    dm_user = SimpleNamespace(send=AsyncMock())
    interaction = _interaction(caller_id="111", dm_user=dm_user)
    tree = _RecordingTree()
    ban_user.register(tree, database, _settings(), policy_service=build_test_policy_service(database, _settings()))
    callback = tree.commands["ban-user"].callback

    await callback(interaction, user="Alice@bridge.example:8443", community="cats", reason="first")
    original_id = _active_bans(database)[0].id
    removed = unban_user_operation(
        UnbanUserInput(
            database,
            _settings(),
            "111",
            10,
            "cats",
            "Alice@bridge.example:8443",
            build_test_policy_service(database, _settings()),
        )
    )
    await callback(interaction, user="Alice@bridge.example:8443", community="cats", reason="second")

    active = _active_bans(database)
    assert removed.applied is True
    assert len(active) == 1
    assert active[0].id == original_id
    assert active[0].local_community_id == community.id
    assert active[0].reason == "second"
    assert _audit_actions(database) == ["ban.created", "ban.removed", "ban.reactivated"]
    assert dm_user.send.await_count == 2


@pytest.mark.asyncio
async def test_dm_failure_does_not_roll_back_committed_ban_or_audit(tmp_path: Path) -> None:
    """Discord DM failure remains outside the authoritative management transaction."""
    database = build_database(tmp_path, "plan93-command-dm-failure.db")
    _local_community(database)
    _registered_local_user(database)
    dm_user = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("DMs closed")))
    interaction = _interaction(caller_id="111", dm_user=dm_user)
    tree = _RecordingTree()
    ban_user.register(tree, database, _settings(), policy_service=build_test_policy_service(database, _settings()))

    await tree.commands["ban-user"].callback(
        interaction,
        user="Alice@bridge.example:8443",
        community="cats",
        reason="spam",
    )

    assert len(_active_bans(database)) == 1
    assert _audit_actions(database) == ["ban.created"]
    interaction.response.send_message.assert_awaited_once()


def _global_ban(database: Database, *, discord_user_id: str = "123", reason: str = "abuse") -> None:
    """Seed one global local-user ban through the production management action."""
    user = database.users.get_user_by_discord_user_id(discord_user_id)
    database.management_actions.create_or_reactivate_ban(
        actor_discord_user_id="999",
        local_community_id=None,
        actor_handle=f"{user.activitypub_username}@bridge.example:8443",
        actor_url=user.actor_url,
        target_discord_user_id=discord_user_id,
        reason=reason,
    )


@pytest.mark.asyncio
async def test_global_ban_rejects_remote_subscription_starter_before_any_publish_artifact(tmp_path: Path) -> None:
    """A blocked starter receives one source reply and creates no outbound state."""
    database = build_database(tmp_path, "plan93-global-starter.db")
    add_accepted_subscription(database)
    _registered_local_user(database)
    _global_ban(database)
    gateway = AsyncMock()
    gateway.publish_content.return_value = build_publish_result(kind="post")
    service = ContentPublishService(
        database=database,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
        settings=_settings(),
            bridge_policy_service=build_test_policy_service(database),
)
    starter = build_starter_message(display_name="Changed nickname")

    result = await service.publish_thread_starter(thread=build_thread(), starter_message=starter)

    assert result.status == "rejected"
    assert result.reason == "user_banned"
    starter.reply.assert_awaited_once_with(
        "You were banned from this bridge instance.\nReason: abuse"
    )
    gateway.publish_content.assert_not_awaited()
    assert database.message_mappings.get_message_mapping_by_discord_message_id(starter.id) is None


@pytest.mark.asyncio
async def test_global_ban_rejects_remote_subscription_comment_before_any_publish_artifact(tmp_path: Path) -> None:
    """A blocked reply cannot create a comment object or alter source mappings."""
    database = build_database(tmp_path, "plan93-global-comment.db")
    add_accepted_subscription(database)
    _registered_local_user(database)
    _global_ban(database)
    create_source_thread_group(database)
    gateway = AsyncMock()
    service = ContentPublishService(
        database=database,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
        settings=_settings(),
            bridge_policy_service=build_test_policy_service(database),
)
    message = build_thread_message(message_id=301)

    result = await service.publish_thread_message(message=message)

    assert result.status == "rejected"
    assert result.reason == "user_banned"
    message.reply.assert_awaited_once_with(
        "You were banned from this bridge instance.\nReason: abuse"
    )
    gateway.publish_content.assert_not_awaited()
    assert database.message_mappings.get_message_mapping_by_discord_message_id(message.id) is None


@pytest.mark.asyncio
async def test_community_ban_blocks_only_selected_local_community_and_global_ban_takes_precedence(tmp_path: Path) -> None:
    """Scoped policy stays isolated, while an added global row becomes authoritative."""
    database = build_database(tmp_path, "plan93-community-scope.db")
    cats = _local_community(database, slug="cats")
    dogs = _local_community(database, slug="dogs")
    user = _registered_local_user(database)
    database.management_actions.create_or_reactivate_ban(
        actor_discord_user_id="111",
        local_community_id=cats.id,
        actor_handle="Alice@bridge.example:8443",
        actor_url=user.actor_url,
        target_discord_user_id="123",
        reason="cats-only",
    )
    gateway = AsyncMock()
    gateway.publish_local_community_content.return_value = build_publish_result(kind="post")
    service = ContentPublishService(
        database=database,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
        settings=_settings(),
            bridge_policy_service=build_test_policy_service(database),
)
    cats_message = build_starter_message(message_id=310)
    dogs_message = build_starter_message(message_id=311)

    cats_result = await service.publish_local_thread_starter(
        thread=build_thread(thread_id=210),
        starter_message=cats_message,
        community_actor_url=cats.actor_url,
    )
    dogs_result = await service.publish_local_thread_starter(
        thread=build_thread(thread_id=211),
        starter_message=dogs_message,
        community_actor_url=dogs.actor_url,
    )
    _global_ban(database, reason="global")
    global_message = build_starter_message(message_id=312)
    global_result = await service.publish_local_thread_starter(
        thread=build_thread(thread_id=212),
        starter_message=global_message,
        community_actor_url=cats.actor_url,
    )

    assert cats_result.reason == "user_banned"
    cats_message.reply.assert_awaited_once_with(
        "You were banned from community cats@bridge.example:8443.\nReason: cats-only"
    )
    assert dogs_result.status == "published"
    assert database.message_mappings.get_message_mapping_by_discord_message_id(311) is not None
    assert global_result.reason == "user_banned"
    global_message.reply.assert_awaited_once_with(
        "You were banned from this bridge instance.\nReason: global"
    )


@pytest.mark.asyncio
async def test_publish_ban_lookup_failure_is_fail_closed_without_false_ban_claim(tmp_path: Path) -> None:
    """Database uncertainty rejects publication with a generic temporary error."""
    database = build_database(tmp_path, "plan93-fail-closed.db")
    add_accepted_subscription(database)
    _registered_local_user(database)
    database.community_actor_bans.get_active_global_ban_by_discord_user_id = Mock(
        side_effect=RuntimeError("database unavailable")
    )
    gateway = AsyncMock()
    service = ContentPublishService(
        database=database,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
        settings=_settings(),
            bridge_policy_service=build_test_policy_service(database),
)
    starter = build_starter_message()

    result = await service.publish_thread_starter(thread=build_thread(), starter_message=starter)

    assert result.reason == "ban_check_failed"
    starter.reply.assert_awaited_once_with(
        "The bridge could not verify publishing access. Please try again later."
    )
    gateway.publish_content.assert_not_awaited()
    assert database.message_mappings.get_message_mapping_by_discord_message_id(starter.id) is None


@pytest.mark.asyncio
async def test_discord_fanout_uses_registered_handle_not_mutable_nickname(tmp_path: Path) -> None:
    """A real mirror delivery renders the canonical local handle in its header."""
    database = build_database(tmp_path, "plan93-header.db")
    _registered_local_user(database)
    forum = build_forum_channel_object_result(channel_id=500, thread_id=600, starter_message_id=700)

    async def fetch_forum_channel(channel_id: int) -> object:
        assert channel_id == 500
        return forum

    tracker = SimpleNamespace(
        track_message_edit=Mock(),
        track_message_delete=Mock(),
    )
    fanout = DiscordFanout(
        mutation_tracker=tracker,
        database=database,
        policy_service=build_test_policy_service(database, _settings()),
        bot=SimpleNamespace(
            database=database,
            settings=_settings(),
            fetch_forum_channel=fetch_forum_channel,
                    bridge_policy_service=build_test_policy_service(database, _settings()),
)
    )
    source = build_starter_message(content="hello", display_name="Changed nickname")

    results = await fanout.mirror_thread_to_siblings(
        source_thread=build_thread(name="Topic"),
        source_starter_message=source,
        sibling_channel_ids=[500],
    )

    assert len(results) == 1
    forum.create_thread.assert_awaited_once_with(
        name="Topic",
        content="`Alice@bridge.example:8443`\n\nhello",
    )


def test_legacy_schema_migrates_through_public_database_startup_and_is_idempotent(tmp_path: Path) -> None:
    """Normal startup upgrades legacy rows without loss or duplication."""
    path = tmp_path / "plan93-legacy.db"
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
            ) VALUES (
                7, 'bob@remote.example', NULL, 'inactive', '999', 'old reason',
                '2026-01-01 00:00:00', '2026-02-01 00:00:00'
            )
        """))

    database = Database(f"sqlite:///{path}")
    database.create_all()
    database.migrate()
    database.migrate()

    with database.session() as session:
        rows = list(session.scalars(select(CommunityActorBan)))
    assert len(rows) == 1
    assert rows[0].scope == "community"
    assert rows[0].scope_key == "7"
    assert rows[0].reason == "old reason"
    assert rows[0].created_by_discord_user_id == "999"
