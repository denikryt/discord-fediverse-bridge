"""Behavior scenarios for Stage 1 local-subscriber control-plane cleanup.

These scenarios intentionally stop at subscription persistence and public
presentation. They do not assume any participant sync behavior yet; later
stages will extend the runtime once the participant model is explicit.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from src.commands import list_subs, subscribe, unsubscribe
from src.db import Database
from src.local_communities.service import LocalCommunityService
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


def _database(tmp_path: Path) -> Database:
    """Create one isolated SQLite database for Stage 1 subscriber scenarios."""
    database = Database(f"sqlite:///{tmp_path / 'stage1-local-subscribers.db'}")
    database.create_all()
    return database


def _settings() -> SimpleNamespace:
    """Build the minimum settings contract needed for local-bridge resolution."""
    return SimpleNamespace(
        federation_allowlist=[],
        normalized_public_bridge_base_url=f"https://{BRIDGE_EXAMPLE_DOMAIN}",
        normalized_fedify_origin=f"https://{BRIDGE_EXAMPLE_DOMAIN}",
        fedify_origin=f"https://{BRIDGE_EXAMPLE_DOMAIN}",
        fedify_actor_identifier="bridge",
        fedify_shared_secret="test-secret",
        registration_session_cookie_name="bridge_registration_session",
        registration_session_ttl_seconds=3600,
    )


def _register_user(database: Database, discord_user_id: str = "1234567890") -> None:
    """Seed one registered moderator for subscribe-community authorization."""
    actor_url = f"https://{BRIDGE_EXAMPLE_DOMAIN}/users/alice"
    database.users.create_user(
        discord_user_id=discord_user_id,
        activitypub_username="alice",
        actor_url=actor_url,
        inbox_url=f"{actor_url}/inbox",
        outbox_url=f"{actor_url}/outbox",
        followers_url=f"{actor_url}/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )


def _create_local_community(database: Database, *, forum_channel_id: int = 100) -> object:
    """Create one bridge-owned local community for local-subscriber tests."""
    LocalCommunityService(
        database=database,
        base_url=f"https://{BRIDGE_EXAMPLE_DOMAIN}",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=99999,
        discord_forum_channel_id=forum_channel_id,
        slug="great_community",
        name="Great Community",
        description="A bridge-owned local community.",
        created_by_discord_user_id="123",
    )
    community = database.local_communities.get_local_community_by_slug("great_community")
    assert community is not None
    return community


@pytest.mark.asyncio
async def test_subscribe_community_persists_local_subscriber_without_remote_follow(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    fedify_gateway,
) -> None:
    """A same-instance local-bridge target should create only local subscriber state."""
    database = _database(tmp_path)
    _register_user(database)
    community = _create_local_community(database, forum_channel_id=100)

    subscribe.register(command_tree, database, fedify_gateway, _settings())

    command = command_tree.commands["subscribe-community"]
    with patch("src.commands.subscribe.resolve_selected_community") as resolve_selected:
        resolve_selected.return_value = SimpleNamespace(
            source="local_bridge",
            actor_id=community.actor_url,
            name=community.display_name,
            numeric_id=None,
            handle=f"!{community.slug}@{BRIDGE_EXAMPLE_DOMAIN}",
            local_community_id=community.id,
            remote_software="discord-fediverse-bridge",
        )
        await command.callback(
            interaction,
            f"https://{BRIDGE_EXAMPLE_DOMAIN}",
            community.actor_url,
            forum_channel,
        )

    local_subscriber = database.local_subscribers.get_local_subscriber_by_channel(forum_channel.id)
    assert local_subscriber is not None
    assert local_subscriber.local_community_id == community.id
    assert local_subscriber.status == "active"
    assert database.bridge_actor_follows.get_bridge_actor_follow(community.actor_url) is None
    assert database.remote_subscribers.get_remote_subscriber(
        local_community_id=community.id,
        remote_actor_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/actors/bridge",
    ) is None
    fedify_gateway.follow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "Subscribed <#12345> to local community **Great Community**.",
        ephemeral=False,
    )


@pytest.mark.asyncio
async def test_subscribe_community_rejects_host_forum_as_local_subscriber_target(
    tmp_path: Path,
    command_tree,
    interaction,
    fedify_gateway,
) -> None:
    """The host forum must not also be persisted as a local subscriber."""
    database = _database(tmp_path)
    _register_user(database)
    community = _create_local_community(database, forum_channel_id=12345)
    host_forum = SimpleNamespace(id=12345, mention="<#12345>")

    subscribe.register(command_tree, database, fedify_gateway, _settings())

    command = command_tree.commands["subscribe-community"]
    with patch("src.commands.subscribe.resolve_selected_community") as resolve_selected:
        resolve_selected.return_value = SimpleNamespace(
            source="local_bridge",
            actor_id=community.actor_url,
            name=community.display_name,
            numeric_id=None,
            handle=f"!{community.slug}@{BRIDGE_EXAMPLE_DOMAIN}",
            local_community_id=community.id,
            remote_software="discord-fediverse-bridge",
        )
        await command.callback(
            interaction,
            f"https://{BRIDGE_EXAMPLE_DOMAIN}",
            community.actor_url,
            host_forum,
        )

    assert database.local_subscribers.get_local_subscriber_by_channel(host_forum.id) is None
    fedify_gateway.follow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "Forum channel <#12345> is already used by another bridge community or subscription.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_unsubscribe_community_removes_only_local_subscriber_state(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    fedify_gateway,
) -> None:
    """Removing a local subscriber should not dispatch remote Undo(Follow)."""
    database = _database(tmp_path)
    community = _create_local_community(database, forum_channel_id=100)
    database.local_subscribers.create_local_subscriber(
        local_community_id=community.id,
        discord_guild_id=interaction.guild_id,
        discord_channel_id=forum_channel.id,
        initiated_by_discord_user_id=str(interaction.user.id),
        status="active",
    )

    unsubscribe.register(command_tree, database, fedify_gateway, _settings())

    command = command_tree.commands["unsubscribe-channel"]
    await command.callback(interaction, forum_channel)

    assert database.local_subscribers.get_local_subscriber_by_channel(forum_channel.id) is None
    fedify_gateway.unfollow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "Unsubscribed <#12345> from local community **Great Community**.",
        ephemeral=False,
    )


@pytest.mark.asyncio
async def test_list_subscriptions_renders_remote_and_local_sections(
    tmp_path: Path,
    command_tree,
    interaction,
) -> None:
    """The list command should separate remote subscriptions from local subscribers."""
    database = _database(tmp_path)
    community = _create_local_community(database, forum_channel_id=100)
    database.remote_subscriptions.create_subscription(
        discord_channel_id=222,
        discord_guild_id=interaction.guild_id,
        lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/worldnews",
        lemmy_community_name="worldnews",
        lemmy_community_id=777,
        community_handle=f"!worldnews@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/worldnews/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/222",
        initiated_by_discord_user_id="4444",
        status="accepted",
    )
    database.local_subscribers.create_local_subscriber(
        local_community_id=community.id,
        discord_guild_id=interaction.guild_id,
        discord_channel_id=333,
        initiated_by_discord_user_id="5555",
        status="active",
    )

    list_subs.register(command_tree, database)

    command = command_tree.commands["list-subscriptions"]
    await command.callback(interaction)

    send_call = interaction.response.send_message.await_args
    embed = send_call.kwargs["embed"]

    assert isinstance(embed, discord.Embed)
    assert "Remote community subscriptions" in embed.description
    assert "Local community subscribers" in embed.description
    assert "• <#222> → **worldnews**" in embed.description
    assert "• <#333> → **!great_community@bridge.example**" in embed.description
    assert send_call.kwargs["ephemeral"] is True
