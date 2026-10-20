"""personal-feed binding behavior scenarios for personal-feed ownership and routing admission."""

from __future__ import annotations

from types import SimpleNamespace

import discord
from sqlalchemy import inspect

from src.db import Database
from src.discord_channel_types import (
    is_normal_guild_text_channel,
    is_personal_feed_root_channel,
)
from src.models import ChannelCommunitySubscription, User
from src.personal_feeds import PersonalFeedService


def _user(discord_id: str, username: str) -> User:
    """Build one registered User Actor with deterministic identity fields."""
    return User(
        discord_user_id=discord_id,
        activitypub_username=username,
        actor_url=f"https://bridge.test/actors/users/{username}",
        inbox_url=f"https://bridge.test/actors/users/{username}/inbox",
        outbox_url=f"https://bridge.test/actors/users/{username}/outbox",
        followers_url=f"https://bridge.test/actors/users/{username}/followers",
        public_key_pem="public",
        private_key_pem="private",
    )


def test_migration_creates_personal_feed_table_and_restart_restores_binding(
    tmp_path,
) -> None:
    """An additive migration must persist the current binding across DB reopen."""
    path = tmp_path / "bridge.sqlite3"
    database = Database(f"sqlite:///{path}")
    database.migrate()
    with database.session() as session:
        session.add(_user("10", "alice"))
    service = PersonalFeedService(database)

    assert service.bind_or_move("10", 100, 200).status == "bound"
    assert "personal_feed_bindings" in inspect(database.engine).get_table_names()
    database.close()

    reopened = Database(f"sqlite:///{path}")
    reopened.migrate()
    binding = PersonalFeedService(reopened).resolve_root_binding(100, 200)
    assert binding is not None
    assert binding.discord_user_id == "10"
    reopened.close()


def test_move_conflict_preserves_previous_binding(tmp_path) -> None:
    """A target owned by another bridge mode cannot destroy the caller's old feed."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.sqlite3'}")
    database.migrate()
    with database.session() as session:
        session.add(_user("10", "alice"))
    database.remote_subscriptions.create_subscription(
        discord_channel_id=300,
        discord_guild_id=3000,
        lemmy_community_actor_id="https://remote.test/c/group",
        lemmy_community_name="group",
        lemmy_community_id=None,
    )
    service = PersonalFeedService(database)
    assert service.bind_or_move("10", 1000, 200).status == "bound"

    result = service.bind_or_move("10", 3000, 300)

    assert result.status == "channel_conflict"
    assert result.conflict_kind == "remote_subscription"
    assert service.resolve_root_binding(1000, 200) is not None
    assert service.resolve_root_binding(3000, 300) is None


def test_one_binding_per_guild_and_idempotent_unbind(tmp_path) -> None:
    """The guild invariant rejects a second owner and unbind remains repeat-safe."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.sqlite3'}")
    database.migrate()
    with database.session() as session:
        session.add_all([_user("10", "alice"), _user("20", "bob")])
    service = PersonalFeedService(database)
    assert service.bind_or_move("10", 100, 200).status == "bound"

    assert service.bind_or_move("20", 100, 201).status == "guild_conflict"
    assert service.unbind("10").status == "unbound"
    assert service.unbind("10").status == "not_bound"


def test_channel_predicates_keep_strict_and_personal_feed_boundaries() -> None:
    """Personal feeds admit text-like roots while the legacy strict predicate remains narrow."""
    ordinary = object.__new__(discord.TextChannel)
    ordinary._type = discord.ChannelType.text.value
    announcement = object.__new__(discord.TextChannel)
    # Discord API GUILD_ANNOUNCEMENT; discord.py exposes it as news.
    announcement._type = discord.ChannelType.news.value
    forged_text = SimpleNamespace(type=discord.ChannelType.text)
    # Discord API GUILD_ANNOUNCEMENT; discord.py exposes it as news.
    forged_announcement = SimpleNamespace(type=discord.ChannelType.news)

    assert is_normal_guild_text_channel(ordinary) is True
    assert is_normal_guild_text_channel(announcement) is False
    assert is_normal_guild_text_channel(forged_text) is False
    assert is_personal_feed_root_channel(ordinary) is True
    assert is_personal_feed_root_channel(announcement) is True
    assert is_personal_feed_root_channel(forged_text) is False
    assert is_personal_feed_root_channel(forged_announcement) is False


def test_repository_lookups_and_active_binding_survive_move_unbind_and_reopen(
    tmp_path,
) -> None:
    """Every personal-feed binding lookup works through the repository API and current-state lifecycle."""
    from src.discord_event_router import DiscordEventRouter

    path = tmp_path / "bridge.sqlite3"
    database = Database(f"sqlite:///{path}")
    database.migrate()
    with database.session() as session:
        session.add(_user("10", "alice"))
    service = PersonalFeedService(database)
    user = database.users.get_user_by_discord_user_id("10")
    assert user is not None
    router = DiscordEventRouter(
        database=database,
        community_runtime=SimpleNamespace(),
        local_community_runtime=SimpleNamespace(),
        bridge_policy_service=SimpleNamespace(
            is_discord_guild_allowed=lambda _guild_id: True
        ),
        personal_feed_service=service,
    )

    assert database.personal_feed_bindings.get_by_user_id(user.id) is None
    assert database.personal_feed_bindings.get_by_discord_user_id("10") is None
    assert database.personal_feed_bindings.get_by_channel_id(200) is None
    assert database.personal_feed_bindings.get_by_guild_id(100) is None
    assert (
        database.personal_feed_bindings.is_current_channel_for_user(user.id, 200)
        is False
    )
    assert router.is_active_personal_feed_channel(user.id, 200) is False

    created = service.bind_or_move("10", 100, 200)
    assert created.status == "bound"
    assert created.binding is not None
    assert (
        database.personal_feed_bindings.get_by_user_id(user.id).discord_channel_id
        == 200
    )
    assert (
        database.personal_feed_bindings.get_by_discord_user_id("10").discord_channel_id
        == 200
    )
    assert database.personal_feed_bindings.get_by_channel_id(200).user_id == user.id
    assert database.personal_feed_bindings.get_by_guild_id(100).user_id == user.id
    assert (
        database.personal_feed_bindings.is_current_channel_for_user(user.id, 200)
        is True
    )
    assert router.is_active_personal_feed_channel(user.id, 200) is True

    moved = service.bind_or_move("10", 101, 201, expected_binding_id=created.binding.id)
    assert moved.status == "moved"
    assert router.is_active_personal_feed_channel(user.id, 200) is False
    assert router.is_active_personal_feed_channel(user.id, 201) is True

    database.close()
    reopened = Database(f"sqlite:///{path}")
    reopened.migrate()
    reopened_service = PersonalFeedService(reopened)
    reopened_router = DiscordEventRouter(
        database=reopened,
        community_runtime=SimpleNamespace(),
        local_community_runtime=SimpleNamespace(),
        bridge_policy_service=SimpleNamespace(
            is_discord_guild_allowed=lambda _guild_id: True
        ),
        personal_feed_service=reopened_service,
    )
    assert reopened_router.is_active_personal_feed_channel(user.id, 201) is True
    assert reopened_service.unbind("10").status == "unbound"
    assert reopened_router.is_active_personal_feed_channel(user.id, 201) is False
    reopened.close()


def _run_concurrent_binds(path, requests):
    """Start separate Database/service instances at one first-bind barrier."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(len(requests))

    def invoke(request):
        discord_user_id, guild_id, channel_id = request
        database = Database(f"sqlite:///{path}")
        service = PersonalFeedService(database)
        barrier.wait()
        try:
            return service.bind_or_move(
                discord_user_id,
                guild_id,
                channel_id,
                expected_binding_id=None,
            ).status
        finally:
            database.close()

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return list(pool.map(invoke, requests))


def test_concurrent_first_binds_one_user_two_guilds_report_one_success(
    tmp_path,
) -> None:
    """A stale concurrent first-bind cannot silently become an intentional move."""
    path = tmp_path / "bridge.sqlite3"
    database = Database(f"sqlite:///{path}")
    database.migrate()
    with database.session() as session:
        session.add(_user("10", "alice"))
    database.close()

    statuses = _run_concurrent_binds(path, [("10", 100, 200), ("10", 101, 201)])

    assert statuses.count("bound") == 1
    assert statuses.count("binding_changed") == 1


def test_concurrent_first_binds_two_users_same_guild_report_one_success(
    tmp_path,
) -> None:
    """The DB-backed guild invariant gives one winner across service instances."""
    path = tmp_path / "bridge.sqlite3"
    database = Database(f"sqlite:///{path}")
    database.migrate()
    with database.session() as session:
        session.add_all([_user("10", "alice"), _user("20", "bob")])
    database.close()

    statuses = _run_concurrent_binds(path, [("10", 100, 200), ("20", 100, 201)])

    assert statuses.count("bound") == 1
    assert (
        len(
            [
                status
                for status in statuses
                if status in {"guild_conflict", "channel_conflict"}
            ]
        )
        == 1
    )


def test_concurrent_first_binds_two_users_same_channel_report_one_success(
    tmp_path,
) -> None:
    """The DB-backed channel invariant gives one winner across service instances."""
    path = tmp_path / "bridge.sqlite3"
    database = Database(f"sqlite:///{path}")
    database.migrate()
    with database.session() as session:
        session.add_all([_user("10", "alice"), _user("20", "bob")])
    database.close()

    statuses = _run_concurrent_binds(path, [("10", 100, 200), ("20", 101, 200)])

    assert statuses.count("bound") == 1
    assert (
        len(
            [
                status
                for status in statuses
                if status in {"guild_conflict", "channel_conflict"}
            ]
        )
        == 1
    )
