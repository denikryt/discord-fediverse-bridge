"""Behavior scenarios for Discord directory snapshot writes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.commands import create_community, subscribe
from src.db import Database
from src.discord_bot import BridgeBot
from src.discord_directory import refresh_discord_directory_from_bot
from src.local_communities.service import LocalCommunityService
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite database for snapshot behavior tests."""
    database = Database(f"sqlite:///{tmp_path / 'directory.db'}")
    database.create_all()
    return database


def _settings() -> SimpleNamespace:
    """Build settings required by command adapters in these scenarios."""
    return SimpleNamespace(
        federation_allowlist=[],
        normalized_public_bridge_base_url=f"https://{BRIDGE_EXAMPLE_DOMAIN}",
        normalized_fedify_origin=f"https://{BRIDGE_EXAMPLE_DOMAIN}",
        fedify_origin=f"https://{BRIDGE_EXAMPLE_DOMAIN}",
        local_community_operator_allowlist=["1234567890"],
    )


def _interaction() -> AsyncMock:
    """Build a Discord interaction fake with guild and response surfaces."""
    interaction = AsyncMock()
    interaction.user.id = "1234567890"
    interaction.guild_id = 99999
    interaction.guild = SimpleNamespace(id=99999, name="Guild Before")
    interaction.response.send_message = AsyncMock()
    interaction.namespace = SimpleNamespace(instance_domain=f"https://{LEMMY_EXAMPLE_DOMAIN}")
    return interaction


def _forum_channel(channel_id: int = 12345, name: str = "forum-before") -> SimpleNamespace:
    """Build a forum-channel fake with the attributes command adapters use."""
    return SimpleNamespace(id=channel_id, name=name, mention=f"<#{channel_id}>")


def _create_bridge_user(database: Database) -> None:
    """Persist the registered bridge user required by subscribe operations."""
    database.users.create_user(
        discord_user_id="1234567890",
        activitypub_username="alice",
        actor_url=f"https://{BRIDGE_EXAMPLE_DOMAIN}/users/alice",
        inbox_url=f"https://{BRIDGE_EXAMPLE_DOMAIN}/users/alice/inbox",
        outbox_url=f"https://{BRIDGE_EXAMPLE_DOMAIN}/users/alice/outbox",
        followers_url=f"https://{BRIDGE_EXAMPLE_DOMAIN}/users/alice/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )


@pytest.mark.asyncio
async def test_create_community_command_stores_guild_and_forum_snapshots(
    tmp_path: Path,
    command_tree,
) -> None:
    """A successful `/create_community` captures the host guild/forum labels."""
    database = _database(tmp_path)
    _create_bridge_user(database)
    interaction = _interaction()
    forum = _forum_channel(name="community-host")
    create_community.register(command_tree, database, _settings())

    command = command_tree.commands["create_community"]
    interaction.response.send_modal = AsyncMock()
    await command.callback(interaction)
    modal = interaction.response.send_modal.await_args.args[0]
    modal.slug_input._value = "hackers"
    modal.display_name_input._value = "Hackers"
    modal.summary_input._value = "A local forum"
    modal.channel_select._values = [forum]
    await modal.on_submit(interaction)

    guild_snapshot = database.discord_directory.get_guild_snapshot(99999)
    channel_snapshot = database.discord_directory.get_channel_snapshot(forum.id)
    assert guild_snapshot.guild_name == "Guild Before"
    assert channel_snapshot.channel_name == "community-host"
    assert channel_snapshot.discord_guild_id == 99999


@pytest.mark.asyncio
async def test_subscribe_community_stores_snapshots_for_remote_subscription(
    tmp_path: Path,
    command_tree,
) -> None:
    """A successful remote `/subscribe-community` captures subscribed forum labels."""
    database = _database(tmp_path)
    _create_bridge_user(database)
    interaction = _interaction()
    forum = _forum_channel(name="lemmy-news")
    fedify_gateway = AsyncMock()
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/news"
    fedify_gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=community_actor_url,
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/news",
    )
    subscribe.register(command_tree, database, fedify_gateway, _settings())

    command = command_tree.commands["subscribe-community"]
    await command.callback(
        interaction,
        community=f"{community_actor_url}|news|1",
        channel=forum,
        instance_domain=f"https://{LEMMY_EXAMPLE_DOMAIN}",
    )

    assert database.discord_directory.get_guild_snapshot(99999).guild_name == "Guild Before"
    assert database.discord_directory.get_channel_snapshot(forum.id).channel_name == "lemmy-news"


@pytest.mark.asyncio
async def test_subscribe_community_stores_snapshots_for_local_subscription(
    tmp_path: Path,
    command_tree,
) -> None:
    """A successful same-instance subscribe captures local subscriber forum labels."""
    database = _database(tmp_path)
    _create_bridge_user(database)
    LocalCommunityService(
        database=database,
        base_url=f"https://{BRIDGE_EXAMPLE_DOMAIN}",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=99999,
        discord_forum_channel_id=9000,
        slug="hackers",
        name="Hackers",
        description="A local forum",
        created_by_discord_user_id="123",
    )
    community = database.local_communities.get_local_community_by_slug("hackers")
    interaction = _interaction()
    forum = _forum_channel(channel_id=9001, name="mirror-forum")
    fedify_gateway = AsyncMock()
    resolved = SimpleNamespace(
        source="local_bridge",
        actor_id=community.actor_url,
        name="Hackers",
        numeric_id=None,
        handle=f"!hackers@{BRIDGE_EXAMPLE_DOMAIN}",
        local_community_id=community.id,
        remote_software="bridge",
    )
    subscribe.register(command_tree, database, fedify_gateway, _settings())

    command = command_tree.commands["subscribe-community"]
    with patch("src.commands.subscribe.resolve_selected_community", new=AsyncMock(return_value=resolved)):
        await command.callback(
            interaction,
            community=f"!hackers@{BRIDGE_EXAMPLE_DOMAIN}",
            channel=forum,
            instance_domain=f"https://{BRIDGE_EXAMPLE_DOMAIN}",
        )

    assert database.discord_directory.get_channel_snapshot(forum.id).channel_name == "mirror-forum"
    assert database.local_subscribers.get_local_subscriber_by_channel(forum.id) is not None


@pytest.mark.asyncio
async def test_bridge_bot_on_ready_refreshes_renamed_guild_and_forum_snapshots(
    tmp_path: Path,
) -> None:
    """Bot startup refresh updates existing snapshot rows instead of duplicating them."""
    database = _database(tmp_path)
    database.discord_directory.upsert_guild_snapshot(discord_guild_id=42, guild_name="Old Guild")
    database.discord_directory.upsert_channel_snapshot(
        discord_channel_id=420,
        discord_guild_id=42,
        channel_name="old-forum",
        channel_type="forum",
    )
    forum = SimpleNamespace(id=420, name="new-forum")
    guild = SimpleNamespace(id=42, name="New Guild", forums=[forum])
    bot = SimpleNamespace(guilds=[guild])

    refresh_discord_directory_from_bot(database, bot)

    assert database.discord_directory.get_guild_snapshot(42).guild_name == "New Guild"
    assert database.discord_directory.get_channel_snapshot(420).channel_name == "new-forum"
    assert len(database.discord_directory.list_guild_snapshots([42])) == 1
    assert len(database.discord_directory.list_channel_snapshots([420])) == 1
