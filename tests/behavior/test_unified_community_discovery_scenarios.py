"""Behavior scenarios for unified community discovery in subscribe-channel."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.commands import subscribe
from src.db import Database
from src.local_communities.service import LocalCommunityService


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite repository for discovery behavior scenarios."""
    database = Database(f"sqlite:///{tmp_path / 'behavior-community-discovery.db'}")
    database.create_all()
    return database


def _settings(*, allowlist: list[str], public_bridge_base_url: str, fedify_origin: str) -> SimpleNamespace:
    """Build the minimum settings surface used by community discovery."""
    # These fields mirror the public bridge URLs the command compares against
    # when deciding whether a selected instance belongs to this deployment.
    return SimpleNamespace(
        federation_allowlist=allowlist,
        public_bridge_base_url=public_bridge_base_url,
        fedify_origin=fedify_origin,
        normalized_public_bridge_base_url=public_bridge_base_url.rstrip("/"),
        normalized_fedify_origin=fedify_origin.rstrip("/"),
    )


def _register_user(database: Database, discord_user_id: str = "1234567890") -> None:
    """Create the minimum registered bridge user required by subscribe-channel."""
    database.users.create_user(
        discord_user_id=discord_user_id,
        activitypub_username="alice",
        actor_url="https://bridge.example.com/users/alice",
        inbox_url="https://bridge.example.com/users/alice/inbox",
        outbox_url="https://bridge.example.com/users/alice/outbox",
        followers_url="https://bridge.example.com/users/alice/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )


def _create_local_community(
    database: Database,
    *,
    forum_channel_id: int = 100,
    slug: str = "local-news",
    display_name: str = "Local News",
) -> object:
    """Create one local community row used by same-instance discovery tests."""
    # The discovery command can now transition directly into Stage 1 local
    # subscriber persistence, so the behavior scenario must create the backing
    # LocalCommunity row instead of expecting the removed placeholder branch.
    LocalCommunityService(
        database=database,
        base_url="https://bot.example.com",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=99999,
        discord_forum_channel_id=forum_channel_id,
        slug=slug,
        name=display_name,
        description="Announcements",
    )
    community = database.local_communities.get_local_community_by_slug(slug)
    assert community is not None
    return community


@pytest.mark.asyncio
async def test_same_instance_autocomplete_uses_bridge_discovery(
    tmp_path: Path,
    command_tree,
    database,
    fedify_gateway,
) -> None:
    """Same-instance autocomplete should use bridge discovery instead of Lemmy API."""
    settings = _settings(
        allowlist=[],
        public_bridge_base_url="https://bot.example.com",
        fedify_origin="https://bot.example.com",
    )
    interaction = AsyncMock()
    interaction.namespace = SimpleNamespace(instance_domain="https://bot.example.com")
    subscribe.register(command_tree, database, fedify_gateway, settings)

    with patch("src.commands.subscribe.fetch_bridge_community_summaries", new=AsyncMock()) as fetch_mock:
        with patch("src.commands.subscribe.LemmyClient") as lemmy_client_mock:
            fetch_mock.return_value = [
                SimpleNamespace(
                    id=1,
                    slug="local-news",
                    name="local-news",
                    title="Local News",
                    description="Announcements",
                    actor_id="https://bot.example.com/communities/local-news",
                    alternate_actor_id="https://bot.example.com/c/local-news",
                    handle="!local-news@bot.example.com",
                )
            ]

            choices = await subscribe._community_autocomplete(settings)(interaction, "news")

    lemmy_client_mock.assert_not_called()
    fetch_mock.assert_awaited_once_with("https://bot.example.com")
    assert len(choices) == 1
    assert choices[0].name == "Local News (local-news)"
    assert choices[0].value == "bridge-local:https://bot.example.com/communities/local-news|local-news|1"


@pytest.mark.asyncio
async def test_remote_bridge_handle_uses_remote_follow_path_without_numeric_id(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    fedify_gateway,
) -> None:
    """Remote bridge communities should reuse the remote follow path with numeric_id=None."""
    database = _database(tmp_path)
    _register_user(database)
    settings = _settings(
        allowlist=["remote.bridge.example"],
        public_bridge_base_url="https://bot.example.com",
        fedify_origin="https://bot.example.com",
    )
    remote_actor_id = "https://remote.bridge.example/communities/local-news"
    fedify_gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=remote_actor_id,
        community_inbox_url=f"{remote_actor_id}/inbox",
        follow_activity_id="https://bot.example.com/activities/follow/1",
    )
    subscribe.register(command_tree, database, fedify_gateway, settings)

    with patch("src.commands.subscribe.fetch_bridge_community_summaries", new=AsyncMock()) as fetch_mock:
        with patch("src.commands.subscribe.LemmyClient") as lemmy_client_mock:
            fetch_mock.return_value = [
                SimpleNamespace(
                    id=7,
                    slug="local-news",
                    name="local-news",
                    title="Remote Local News",
                    description="Remote bridge community",
                    actor_id=remote_actor_id,
                    alternate_actor_id="https://remote.bridge.example/c/local-news",
                    handle="!local-news@remote.bridge.example",
                )
            ]

            command = command_tree.commands["subscribe-channel"]
            await command.callback(
                interaction,
                "remote.bridge.example",
                "!local-news@remote.bridge.example",
                forum_channel,
            )

    lemmy_client_mock.assert_not_called()
    fetch_mock.assert_awaited_once_with("https://remote.bridge.example")
    subscription = database.remote_subscriptions.get_subscription_by_channel(forum_channel.id)
    assert subscription is not None
    assert subscription.lemmy_community_actor_id == remote_actor_id
    assert subscription.lemmy_community_id is None
    assert subscription.community_handle == "!local-news@remote.bridge.example"
    fedify_gateway.follow_community.assert_awaited_once_with(remote_actor_id)
    interaction.response.send_message.assert_awaited_once_with(
        "Sent a bridge follow for <#12345> -> **local-news**. Waiting for federation acceptance.",
        ephemeral=False,
    )


@pytest.mark.asyncio
async def test_same_instance_local_actor_url_creates_local_subscriber_state(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    fedify_gateway,
) -> None:
    """Same-instance local discovery should create Stage 1 local-subscriber state."""
    database = _database(tmp_path)
    _register_user(database)
    community = _create_local_community(database, forum_channel_id=100)
    settings = _settings(
        allowlist=["lemmy.world"],
        public_bridge_base_url="https://bot.example.com",
        fedify_origin="https://bot.example.com",
    )
    subscribe.register(command_tree, database, fedify_gateway, settings)

    with patch("src.commands.subscribe.fetch_bridge_community_summaries", new=AsyncMock()) as fetch_mock:
        fetch_mock.return_value = [
            SimpleNamespace(
                id=1,
                slug="local-news",
                name="local-news",
                title="Local News",
                description="Announcements",
                actor_id="https://bot.example.com/communities/local-news",
                alternate_actor_id="https://bot.example.com/c/local-news",
                handle="!local-news@bot.example.com",
            )
        ]

        command = command_tree.commands["subscribe-channel"]
        await command.callback(
            interaction,
            "bot.example.com",
            "https://bot.example.com/communities/local-news",
            forum_channel,
        )

    assert database.remote_subscriptions.get_subscription_by_channel(forum_channel.id) is None
    assert database.bridge_actor_follows.get_bridge_actor_follow("https://bot.example.com/communities/local-news") is None
    local_subscriber = database.local_subscribers.get_local_subscriber(
        local_community_id=community.id,
        discord_channel_id=forum_channel.id,
    )
    assert local_subscriber is not None
    assert local_subscriber.local_community_id == community.id
    assert local_subscriber.discord_channel_id == forum_channel.id
    assert local_subscriber.status == "active"
    fedify_gateway.follow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "Subscribed <#12345> to local community **local-news**.",
        ephemeral=False,
    )
