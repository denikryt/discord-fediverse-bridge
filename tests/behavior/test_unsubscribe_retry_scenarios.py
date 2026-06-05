"""Behavior scenarios for unsubscribe retry preservation and failure messaging."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.commands import unsubscribe
from src.fedify_gateway_client import UnfollowCommunityResult
from src.db import Database
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite repository for unsubscribe retry scenarios."""
    database = Database(f"sqlite:///{tmp_path / 'unsubscribe-retry.db'}")
    database.create_all()
    return database


def _community_actor_url() -> str:
    """Return the shared fake community actor used across retry scenarios."""
    return f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"


def _register_user(database: Database, discord_user_id: str = "1234567890") -> None:
    """Seed the registered command caller required by unsubscribe ingress."""
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


@pytest.mark.asyncio
async def test_last_channel_remote_unfollow_failure_keeps_bridge_follow_for_retry(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    fedify_gateway,
) -> None:
    """Remote Undo failure should preserve retry state after local channel cleanup."""
    database = _database(tmp_path)
    _register_user(database)
    community_actor_url = _community_actor_url()
    follow_activity_id = f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1"
    database.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id=community_actor_url,
        follow_activity_id=follow_activity_id,
        community_inbox_url=f"{community_actor_url}/inbox",
        status="accepted",
    )
    database.remote_subscriptions.create_subscription(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=follow_activity_id,
        initiated_by_discord_user_id="1234567890",
        status="accepted",
    )
    fedify_gateway.unfollow_community.return_value = UnfollowCommunityResult(
        accepted=False,
        error="network error",
    )

    unsubscribe.register(command_tree, database, fedify_gateway, SimpleNamespace(discord_guild_allowlist=[], federation_allowlist=[]))

    command = command_tree.commands["unsubscribe-channel"]
    await command.callback(interaction, forum_channel)

    # Local channel cleanup still applies, but the shared follow row remains so
    # operators can retry the remote Undo(Follow) later.
    assert database.remote_subscriptions.get_subscription_by_channel(forum_channel.id) is None
    assert database.bridge_actor_follows.get_bridge_actor_follow(community_actor_url) is not None
    interaction.response.send_message.assert_awaited_once()
    send_call = interaction.response.send_message.await_args
    assert "remote Undo(Follow) failed" in send_call.args[0]
    assert send_call.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_last_channel_missing_follow_activity_id_blocks_local_cleanup(
    tmp_path: Path,
    command_tree,
    interaction,
    forum_channel,
    fedify_gateway,
) -> None:
    """Missing follow state should stop the last-channel unsubscribe early."""
    database = _database(tmp_path)
    _register_user(database)
    community_actor_url = _community_actor_url()
    database.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id=community_actor_url,
        follow_activity_id=None,
        community_inbox_url=f"{community_actor_url}/inbox",
        status="accepted",
    )
    database.remote_subscriptions.create_subscription(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=None,
        initiated_by_discord_user_id="1234567890",
        status="accepted",
    )

    unsubscribe.register(command_tree, database, fedify_gateway, SimpleNamespace(discord_guild_allowlist=[], federation_allowlist=[]))

    command = command_tree.commands["unsubscribe-channel"]
    await command.callback(interaction, forum_channel)

    # Without a follow activity id the bridge cannot perform safe remote
    # cleanup, so the local subscription must remain visible to operators.
    assert database.remote_subscriptions.get_subscription_by_channel(forum_channel.id) is not None
    assert database.bridge_actor_follows.get_bridge_actor_follow(community_actor_url) is not None
    fedify_gateway.unfollow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    send_call = interaction.response.send_message.await_args
    assert "follow activity id is missing" in send_call.args[0]
    assert send_call.kwargs["ephemeral"] is True
