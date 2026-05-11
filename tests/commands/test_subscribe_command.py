from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.commands import subscribe
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


@pytest.mark.asyncio
async def test_subscribe_channel_success(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # A successful subscription should resolve the community, send a real
    # bridge Follow, persist the pending lifecycle state, and return the
    # moderator-facing pending message.
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.get_subscription_by_channel.return_value = None
    lemmy.resolve_community_id.return_value = 777
    fedify_gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=community_actor_url,
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
    )

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"{community_actor_url}|hackers|",
    )

    database.get_subscription_by_channel.assert_called_once_with(forum_channel.id)
    lemmy.resolve_community_id.assert_awaited_once_with(name="hackers")
    database.create_subscription.assert_called_once_with(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
        initiated_by_discord_user_id="1234567890",
        status="pending",
    )
    fedify_gateway.follow_community.assert_awaited_once_with(community_actor_url)
    send_call = interaction.response.send_message.await_args
    assert send_call.args == (
        "Sent a bridge follow for <#12345> -> **hackers**. Waiting for federation acceptance.",
    )
    assert send_call.kwargs.get("ephemeral", False) is False


@pytest.mark.asyncio
async def test_subscribe_channel_rejects_duplicate_accepted(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # Accepted subscriptions do not trigger a second Follow and return an
    # ephemeral message that the channel is already active.
    database.get_subscription_by_channel.return_value = SimpleNamespace(
        status="accepted",
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        lemmy_community_name="hackers",
        lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers|hackers|777",
    )

    database.create_subscription.assert_not_called()
    lemmy.resolve_community_id.assert_not_awaited()
    fedify_gateway.follow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        f"Channel <#12345> is already subscribed to **!hackers@{LEMMY_EXAMPLE_DOMAIN}**.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_channel_rejects_duplicate_pending(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # Pending subscriptions do not trigger a second Follow and tell the
    # moderator that federation acceptance is still outstanding.
    database.get_subscription_by_channel.return_value = SimpleNamespace(
        status="pending",
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        lemmy_community_name="hackers",
        lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers|hackers|777",
    )

    database.create_subscription.assert_not_called()
    fedify_gateway.follow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        f"Channel <#12345> is still waiting for **!hackers@{LEMMY_EXAMPLE_DOMAIN}** to accept the bridge follow.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_channel_rejects_when_community_resolution_fails(
    command_tree,
    interaction,
    forum_channel,
    database,
    lemmy,
    fedify_gateway,
):
    # Manual text input can omit the numeric ID, so a Lemmy resolution failure
    # must stop the flow before any DB mutation is attempted.
    database.get_subscription_by_channel.return_value = None
    lemmy.resolve_community_id.side_effect = RuntimeError("boom")

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers|hackers|",
    )

    database.create_subscription.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "Could not resolve the Lemmy community ID. Please try again.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_channel_marks_failed_when_follow_dispatch_fails(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # Follow dispatch failures must create a failed subscription row so retries
    # are explicit instead of leaving a fake pending state behind.
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.get_subscription_by_channel.return_value = None
    lemmy.resolve_community_id.return_value = 777
    fedify_gateway.follow_community.side_effect = RuntimeError("boom")

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"{community_actor_url}|hackers|",
    )

    database.create_subscription.assert_called_once_with(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=None,
        follow_activity_id=None,
        status="failed",
    )
    interaction.response.send_message.assert_awaited_once_with(
        "Could not subscribe <#12345> to **hackers** because the bridge Follow request failed.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_channel_retries_failed_subscription(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # Failed subscriptions are retriable. The old failed row is removed before
    # the new pending attempt is written.
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.get_subscription_by_channel.return_value = SimpleNamespace(
        status="failed",
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        lemmy_community_name="hackers",
        lemmy_community_actor_id=community_actor_url,
    )
    fedify_gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=community_actor_url,
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/2",
    )

    subscribe.register(command_tree, database, lemmy, fedify_gateway)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"{community_actor_url}|hackers|777",
    )

    database.delete_subscription.assert_called_once_with(forum_channel.id)
    database.create_subscription.assert_called_once_with(
        discord_channel_id=forum_channel.id,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/2",
        initiated_by_discord_user_id="1234567890",
        status="pending",
    )


# ---------------------------------------------------------------------------
# Helpers for allowlist / lemmy_instance tests
# ---------------------------------------------------------------------------


def _settings(allowlist: list[str]) -> SimpleNamespace:
    """Build a minimal settings stub with the given federation_allowlist."""
    return SimpleNamespace(federation_allowlist=allowlist)


def _make_interaction(lemmy_instance: str | None) -> AsyncMock:
    """Build an interaction stub that carries a namespace.lemmy_instance value."""
    mock = AsyncMock()
    mock.response.send_message = AsyncMock()
    mock.user.id = "1234567890"
    mock.namespace = SimpleNamespace(lemmy_instance=lemmy_instance)
    return mock


# ---------------------------------------------------------------------------
# Autocomplete + allowlist tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_autocomplete_uses_lemmy_instance_url(
    command_tree, database, lemmy, fedify_gateway
):
    # When lemmy_instance is provided and the allowlist is empty, autocomplete
    # must create a temporary LemmyClient for that URL, query it, and return
    # its communities. The default injected client must not be called.
    remote_instance_url = "https://lemmy.world"
    interaction = _make_interaction(remote_instance_url)
    remote_communities = [
        {
            "community": {
                "name": "worldnews",
                "title": "World News",
                "actor_id": f"{remote_instance_url}/c/worldnews",
                "id": 42,
            }
        }
    ]

    settings = _settings([])
    subscribe.register(command_tree, database, lemmy, fedify_gateway, settings)
    autocomplete_fn = subscribe._community_autocomplete(lemmy, settings)

    with patch("src.commands.subscribe.LemmyClient") as MockLemmyClient:
        fake_remote = AsyncMock()
        fake_remote.list_communities.return_value = remote_communities
        MockLemmyClient.return_value = fake_remote

        choices = await autocomplete_fn(interaction, "world")

    MockLemmyClient.assert_called_once_with(remote_instance_url)
    fake_remote.list_communities.assert_awaited_once_with(limit=50)
    fake_remote.close.assert_awaited_once()
    lemmy.list_communities.assert_not_awaited()

    assert len(choices) == 1
    assert choices[0].name == "World News (worldnews)"
    assert choices[0].value == f"{remote_instance_url}/c/worldnews|worldnews|42"


@pytest.mark.asyncio
async def test_subscribe_autocomplete_falls_back_to_default_client(
    command_tree, database, lemmy, fedify_gateway
):
    # When lemmy_instance is absent, autocomplete must use the injected default
    # LemmyClient and must not construct a new one.
    interaction = _make_interaction(None)
    default_communities = [
        {
            "community": {
                "name": "tech",
                "title": "Technology",
                "actor_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/c/tech",
                "id": 7,
            }
        }
    ]
    lemmy.list_communities.return_value = default_communities

    settings = _settings([])
    subscribe.register(command_tree, database, lemmy, fedify_gateway, settings)
    autocomplete_fn = subscribe._community_autocomplete(lemmy, settings)

    with patch("src.commands.subscribe.LemmyClient") as MockLemmyClient:
        choices = await autocomplete_fn(interaction, "")

    MockLemmyClient.assert_not_called()
    lemmy.list_communities.assert_awaited_once_with(limit=50)

    assert len(choices) == 1
    assert choices[0].name == "Technology (tech)"
    assert choices[0].value == f"https://{LEMMY_EXAMPLE_DOMAIN}/c/tech|tech|7"


@pytest.mark.asyncio
async def test_subscribe_autocomplete_rejects_unlisted_instance(
    command_tree, database, lemmy, fedify_gateway
):
    # When the allowlist is non-empty and lemmy_instance is not in it,
    # autocomplete must return an empty list without querying Lemmy.
    interaction = _make_interaction("https://forbidden.instance")

    settings = _settings(["allowed.example"])
    subscribe.register(command_tree, database, lemmy, fedify_gateway, settings)
    autocomplete_fn = subscribe._community_autocomplete(lemmy, settings)

    with patch("src.commands.subscribe.LemmyClient") as MockLemmyClient:
        choices = await autocomplete_fn(interaction, "")

    MockLemmyClient.assert_not_called()
    lemmy.list_communities.assert_not_awaited()
    assert choices == []


@pytest.mark.asyncio
async def test_subscribe_channel_rejects_unlisted_lemmy_instance(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # When lemmy_instance is provided but not in the allowlist, the command
    # handler must return an ephemeral error before touching the DB or Lemmy.
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    settings = _settings(["allowed.example"])
    subscribe.register(command_tree, database, lemmy, fedify_gateway, settings)

    command = command_tree.commands["subscribe-channel"]
    await command.callback(
        interaction,
        forum_channel,
        f"{community_actor_url}|hackers|777",
        lemmy_instance="https://forbidden.instance",
    )

    database.create_subscription.assert_not_called()
    lemmy.resolve_community_id.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "Instance **forbidden.instance** is not in the federation allowlist.",
        ephemeral=True,
    )
