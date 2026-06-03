from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.commands import subscribe
from tests_constants import BRIDGE_EXAMPLE_DOMAIN, LEMMY_EXAMPLE_DOMAIN


@pytest.mark.asyncio
async def test_subscribe_community_success(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # A successful subscription should resolve the community, send a real
    # bridge Follow, persist the pending lifecycle state, and return the
    # moderator-facing pending message.
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.remote_subscriptions.get_subscription_by_channel.return_value = None
    fedify_gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=community_actor_url,
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
    )

    subscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["subscribe-community"]
    with patch("src.commands.subscribe.LemmyClient") as MockLemmyClient:
        fake_client = AsyncMock()
        fake_client.resolve_community_id.return_value = 777
        MockLemmyClient.return_value = fake_client
        await command.callback(
            interaction,
            f"https://{LEMMY_EXAMPLE_DOMAIN}",
            f"{community_actor_url}|hackers|",
            forum_channel,
        )

    assert database.remote_subscriptions.get_subscription_by_channel.call_count == 2
    database.remote_subscriptions.get_subscription_by_channel.assert_any_call(forum_channel.id)
    fake_client.resolve_community_id.assert_awaited_once_with(name="hackers")
    database.remote_subscriptions.create_subscription.assert_called_once_with(
        discord_channel_id=forum_channel.id,
        discord_guild_id=interaction.guild_id,
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
        "<@1234567890> subscribed <#12345> to **hackers@lemmy.example**. Waiting for federation acceptance.",
    )
    assert send_call.kwargs.get("ephemeral", False) is False
    assert send_call.kwargs["allowed_mentions"].users is False


@pytest.mark.asyncio
async def test_subscribe_community_rejects_duplicate_accepted(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # Accepted subscriptions do not trigger a second Follow and return an
    # ephemeral message that the channel is already active.
    database.remote_subscriptions.get_subscription_by_channel.return_value = SimpleNamespace(
        status="accepted",
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        lemmy_community_name="hackers",
        lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )

    subscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["subscribe-community"]
    await command.callback(
        interaction,
        f"https://{LEMMY_EXAMPLE_DOMAIN}",
        f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers|hackers|777",
        forum_channel,
    )

    database.remote_subscriptions.create_subscription.assert_not_called()
    fedify_gateway.follow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "Forum channel <#12345> is already used by another bridge community or subscription.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_community_rejects_duplicate_pending(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # Pending subscriptions do not trigger a second Follow and tell the
    # moderator that federation acceptance is still outstanding.
    database.remote_subscriptions.get_subscription_by_channel.return_value = SimpleNamespace(
        status="pending",
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        lemmy_community_name="hackers",
        lemmy_community_actor_id=f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers",
    )

    subscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["subscribe-community"]
    await command.callback(
        interaction,
        f"https://{LEMMY_EXAMPLE_DOMAIN}",
        f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers|hackers|777",
        forum_channel,
    )

    database.remote_subscriptions.create_subscription.assert_not_called()
    fedify_gateway.follow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "Forum channel <#12345> is already used by another bridge community or subscription.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_community_rejects_when_community_resolution_fails(
    command_tree,
    interaction,
    forum_channel,
    database,
    lemmy,
    fedify_gateway,
):
    # Manual text input can omit the numeric ID, so a Lemmy resolution failure
    # must stop the flow before any DB mutation is attempted.
    database.remote_subscriptions.get_subscription_by_channel.return_value = None

    subscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["subscribe-community"]
    with patch("src.commands.subscribe.LemmyClient") as MockLemmyClient:
        fake_client = AsyncMock()
        fake_client.resolve_community_id.side_effect = RuntimeError("boom")
        MockLemmyClient.return_value = fake_client
        await command.callback(
            interaction,
            f"https://{LEMMY_EXAMPLE_DOMAIN}",
            f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers|hackers|",
            forum_channel,
        )

    database.remote_subscriptions.create_subscription.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "Could not resolve the Lemmy community ID. Please try again.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_community_marks_failed_when_follow_dispatch_fails(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # Follow dispatch failures must create a failed subscription row so retries
    # are explicit instead of leaving a fake pending state behind.
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.remote_subscriptions.get_subscription_by_channel.return_value = None
    fedify_gateway.follow_community.side_effect = RuntimeError("boom")

    subscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["subscribe-community"]
    with patch("src.commands.subscribe.LemmyClient") as MockLemmyClient:
        fake_client = AsyncMock()
        fake_client.resolve_community_id.return_value = 777
        MockLemmyClient.return_value = fake_client
        await command.callback(
            interaction,
            f"https://{LEMMY_EXAMPLE_DOMAIN}",
            f"{community_actor_url}|hackers|",
            forum_channel,
        )

    database.remote_subscriptions.create_subscription.assert_called_once_with(
        discord_channel_id=forum_channel.id,
        discord_guild_id=interaction.guild_id,
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
async def test_subscribe_community_retries_failed_subscription(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # Failed subscriptions are retriable. The old failed row is removed before
    # the new pending attempt is written.
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.remote_subscriptions.get_subscription_by_channel.return_value = SimpleNamespace(
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

    subscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["subscribe-community"]
    await command.callback(
        interaction,
        f"https://{LEMMY_EXAMPLE_DOMAIN}",
        f"{community_actor_url}|hackers|777",
        forum_channel,
    )

    database.remote_subscriptions.delete_subscription.assert_called_once_with(forum_channel.id)
    database.remote_subscriptions.create_subscription.assert_called_once_with(
        discord_channel_id=forum_channel.id,
        discord_guild_id=interaction.guild_id,
        lemmy_community_actor_id=community_actor_url,
        lemmy_community_name="hackers",
        lemmy_community_id=777,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/2",
        initiated_by_discord_user_id=str(interaction.user.id),
        status="pending",
    )
    fedify_gateway.follow_community.assert_awaited_once_with(community_actor_url)
    send_call = interaction.response.send_message.await_args
    assert send_call.args == (
        "<@1234567890> subscribed <#12345> to **hackers@lemmy.example**. Waiting for federation acceptance.",
    )
    assert send_call.kwargs.get("ephemeral", False) is False
    assert send_call.kwargs["allowed_mentions"].users is False


# ---------------------------------------------------------------------------
# Helpers for allowlist / instance_domain tests
# ---------------------------------------------------------------------------


def _settings(allowlist: list[str]) -> SimpleNamespace:
    """Build a minimal settings stub with the given federation_allowlist."""
    return SimpleNamespace(federation_allowlist=allowlist)


def _make_interaction(instance_domain: str | None) -> AsyncMock:
    """Build an interaction stub that carries a namespace.instance_domain value."""
    mock = AsyncMock()
    mock.response.send_message = AsyncMock()
    mock.user.id = "1234567890"
    mock.guild_id = 99999
    mock.namespace = SimpleNamespace(instance_domain=instance_domain)
    return mock


def test_subscribe_command_callback_uses_instance_domain_parameter(
    command_tree, database, fedify_gateway
):
    """The slash-command callback should expose the generic instance parameter name."""
    subscribe.register(command_tree, database, fedify_gateway)

    command = command_tree.commands["subscribe-community"]

    assert "instance_domain" in command.callback.__annotations__
    assert "lemmy_instance" not in command.callback.__annotations__


# ---------------------------------------------------------------------------
# Instance autocomplete tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_instance_autocomplete_returns_allowlist_entries(
    command_tree, database, fedify_gateway
):
    # When federation_allowlist is non-empty, _instance_autocomplete must return
    # one Choice per entry with value = "https://" + hostname.
    settings = _settings(["lemmy.world", "beehaw.org"])
    subscribe.register(command_tree, database, fedify_gateway, settings)
    autocomplete_fn = subscribe._instance_autocomplete(settings)

    interaction = _make_interaction(None)
    choices = await autocomplete_fn(interaction, "")

    assert len(choices) == 2
    assert choices[0].name == "lemmy.world"
    assert choices[0].value == "https://lemmy.world"
    assert choices[1].name == "beehaw.org"
    assert choices[1].value == "https://beehaw.org"


@pytest.mark.asyncio
async def test_subscribe_instance_autocomplete_returns_empty_for_open_federation(
    command_tree, database, fedify_gateway
):
    # When federation_allowlist is empty, _instance_autocomplete must return []
    # so the user types the URL manually.
    settings = _settings([])
    subscribe.register(command_tree, database, fedify_gateway, settings)
    autocomplete_fn = subscribe._instance_autocomplete(settings)

    interaction = _make_interaction(None)
    choices = await autocomplete_fn(interaction, "")

    assert choices == []


# ---------------------------------------------------------------------------
# Community autocomplete + allowlist tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_autocomplete_uses_lemmy_instance_url(
    command_tree, database, lemmy, fedify_gateway
):
    # When instance_domain is provided and the allowlist is empty, autocomplete
    # must create a temporary LemmyClient for that URL, query it, and return
    # its communities.
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
    subscribe.register(command_tree, database, fedify_gateway, settings)
    autocomplete_fn = subscribe._community_autocomplete(settings)

    with patch("src.commands.subscribe.LemmyClient") as MockLemmyClient:
        fake_remote = AsyncMock()
        fake_remote.list_communities.return_value = remote_communities
        MockLemmyClient.return_value = fake_remote

        choices = await autocomplete_fn(interaction, "world")

    MockLemmyClient.assert_called_once_with(remote_instance_url)
    fake_remote.list_communities.assert_awaited_once_with(limit=50, type_="Local")
    fake_remote.close.assert_awaited_once()

    assert len(choices) == 1
    assert choices[0].name == "World News (worldnews@lemmy.world)"
    assert choices[0].value == f"lemmy:{remote_instance_url}/c/worldnews|worldnews|42"


@pytest.mark.asyncio
async def test_subscribe_autocomplete_reads_instance_domain_from_raw_payload_when_namespace_omits_it(
    command_tree, database, lemmy, fedify_gateway
):
    """Community autocomplete should stay instance-scoped after clearing focused text."""
    remote_instance_url = "https://lemmy.world"
    interaction = _make_interaction(None)
    interaction.data = {
        "options": [
            {"name": "instance_domain", "value": "lemmy.world"},
            {"name": "community", "value": "", "focused": True},
        ]
    }
    remote_communities = [
        {
            "community": {
                "name": "worldnews",
                "title": "World News",
                "actor_id": f"{remote_instance_url}/c/worldnews",
                "id": 42,
            },
            "counts": {"users_active_month": 456},
        }
    ]

    settings = _settings([])
    autocomplete_fn = subscribe._community_autocomplete(settings)

    with patch("src.commands.subscribe.LemmyClient") as MockLemmyClient:
        fake_remote = AsyncMock()
        fake_remote.list_communities.return_value = remote_communities
        MockLemmyClient.return_value = fake_remote

        choices = await autocomplete_fn(interaction, "")

    MockLemmyClient.assert_called_once_with(remote_instance_url)
    assert choices[0].name == "World News (worldnews@lemmy.world · 456 active/mo)"
    assert choices[0].value == f"lemmy:{remote_instance_url}/c/worldnews|worldnews|42"


@pytest.mark.asyncio
async def test_subscribe_autocomplete_returns_empty_when_global_cache_is_empty(
    command_tree, database, fedify_gateway
):
    # When instance_domain is absent, autocomplete now uses the global
    # Lemmyverse cache. A cold/failed cache still degrades to empty choices
    # without querying a per-instance Lemmy client.
    class EmptyCache:
        """Cache fake representing a cold failed Lemmyverse refresh."""

        async def get_entries(self):
            """Return no cached global discovery entries."""
            return []

    interaction = _make_interaction(None)

    settings = _settings([])
    subscribe.register(command_tree, database, fedify_gateway, settings)
    autocomplete_fn = subscribe._community_autocomplete(settings, lemmyverse_cache=EmptyCache())

    with patch("src.commands.subscribe.LemmyClient") as MockLemmyClient:
        choices = await autocomplete_fn(interaction, "")

    MockLemmyClient.assert_not_called()
    assert choices == []


@pytest.mark.asyncio
async def test_subscribe_autocomplete_rejects_unlisted_instance(
    command_tree, database, fedify_gateway
):
    # When the allowlist is non-empty and instance_domain is not in it,
    # autocomplete must return an empty list without querying Lemmy.
    interaction = _make_interaction("https://forbidden.instance")

    settings = _settings(["allowed.example"])
    subscribe.register(command_tree, database, fedify_gateway, settings)
    autocomplete_fn = subscribe._community_autocomplete(settings)

    with patch("src.commands.subscribe.LemmyClient") as MockLemmyClient:
        choices = await autocomplete_fn(interaction, "")

    MockLemmyClient.assert_not_called()
    assert choices == []


@pytest.mark.asyncio
async def test_subscribe_community_rejects_unlisted_lemmy_instance(
    command_tree, interaction, forum_channel, database, lemmy, fedify_gateway
):
    # When instance_domain is not in the allowlist, the command handler must
    # return an ephemeral error before touching the DB or Lemmy.
    settings = _settings(["allowed.example"])
    subscribe.register(command_tree, database, fedify_gateway, settings)

    command = command_tree.commands["subscribe-community"]
    await command.callback(
        interaction,
        "https://forbidden.instance",
        f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers|hackers|777",
        forum_channel,
    )

    database.remote_subscriptions.create_subscription.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "Instance **forbidden.instance** is not in the federation allowlist.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_global_autocomplete_uses_lemmyverse_without_instance(
    command_tree, database, fedify_gateway
):
    """No-instance community autocomplete should use cached Lemmyverse actor URLs."""
    from src.lemmyverse_communities import LemmyverseCommunityEntry

    class FakeCache:
        """Small cache fake exposing the get_entries contract used by autocomplete."""

        async def get_entries(self):
            """Return one global Lemmyverse entry without network access."""
            return [
                LemmyverseCommunityEntry(
                    name="technology",
                    title="Technology",
                    actor_id="https://lemmy.world/c/technology",
                    host="lemmy.world",
                    handle="!technology@lemmy.world",
                    active_users_month=123,
                    search_text="technology\ntechnology@lemmy.world\n!technology@lemmy.world\nlemmy.world",
                    feed_order=0,
                )
            ]

    interaction = _make_interaction(None)
    settings = _settings([])
    autocomplete_fn = subscribe._community_autocomplete(settings, lemmyverse_cache=FakeCache())

    with patch("src.commands.subscribe.LemmyClient") as MockLemmyClient:
        with patch("src.commands.subscribe.fetch_bridge_community_summaries", new=AsyncMock()) as fetch_mock:
            choices = await autocomplete_fn(interaction, "tech")

    MockLemmyClient.assert_not_called()
    fetch_mock.assert_not_awaited()
    assert len(choices) == 1
    assert choices[0].name == "Technology (technology@lemmy.world · 123 active/mo)"
    assert choices[0].value == "https://lemmy.world/c/technology"


@pytest.mark.asyncio
async def test_subscribe_global_autocomplete_filters_allowlist(
    command_tree, database, fedify_gateway
):
    """Global autocomplete should hide Lemmyverse communities from blocked hosts."""
    from src.lemmyverse_communities import LemmyverseCommunityEntry

    class FakeCache:
        """Cache fake with allowed and forbidden hosts for policy filtering."""

        async def get_entries(self):
            """Return entries from two hosts so allowlist filtering is observable."""
            return [
                LemmyverseCommunityEntry("news", "News", "https://allowed.example/c/news", "allowed.example", "!news@allowed.example", None, "news allowed.example", 0),
                LemmyverseCommunityEntry("news", "News", "https://blocked.example/c/news", "blocked.example", "!news@blocked.example", None, "news blocked.example", 1),
            ]

    interaction = _make_interaction(None)
    settings = _settings(["allowed.example"])
    autocomplete_fn = subscribe._community_autocomplete(settings, lemmyverse_cache=FakeCache())

    choices = await autocomplete_fn(interaction, "news")

    assert [choice.value for choice in choices] == ["https://allowed.example/c/news"]


@pytest.mark.asyncio
async def test_subscribe_community_rejects_plain_name_without_instance(
    command_tree, interaction, forum_channel, database, fedify_gateway
):
    """Plain community names are ambiguous when no instance_domain is provided."""
    settings = _settings([])
    subscribe.register(command_tree, database, fedify_gateway, settings)

    command = command_tree.commands["subscribe-community"]
    await command.callback(interaction, "technology", forum_channel)

    database.remote_subscriptions.create_subscription.assert_not_called()
    interaction.response.send_message.assert_awaited_once_with(
        "Select a community from autocomplete, paste a full community URL, use !name@instance, or provide instance_domain.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_community_rejects_forbidden_actor_url_without_instance(
    command_tree, interaction, forum_channel, database, fedify_gateway
):
    """Submit must re-check allowlist even for manually supplied actor URLs."""
    settings = _settings(["allowed.example"])
    subscribe.register(command_tree, database, fedify_gateway, settings)

    command = command_tree.commands["subscribe-community"]
    await command.callback(interaction, "https://blocked.example/c/news", forum_channel)

    database.remote_subscriptions.create_subscription.assert_not_called()
    fedify_gateway.follow_community.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once_with(
        "Instance **blocked.example** is not in the federation allowlist.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_subscribe_community_accepts_actor_url_without_instance(
    command_tree, interaction, forum_channel, database, fedify_gateway
):
    """Selected Lemmyverse actor URLs should subscribe without instance_domain."""
    community_actor_url = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
    database.users.get_user_by_discord_user_id.return_value = SimpleNamespace(id=1)
    database.remote_subscriptions.get_subscription_by_channel.return_value = None
    fedify_gateway.follow_community.return_value = SimpleNamespace(
        community_actor_url=community_actor_url,
        community_inbox_url=f"{community_actor_url}/inbox",
        follow_activity_id=f"https://{BRIDGE_EXAMPLE_DOMAIN}/activities/follow/1",
    )
    subscribe.register(command_tree, database, fedify_gateway, _settings([]))

    command = command_tree.commands["subscribe-community"]
    with patch("src.commands.subscribe.LemmyClient") as MockLemmyClient:
        fake_client = AsyncMock()
        fake_client.resolve_community.return_value = {
            "actor_id": community_actor_url,
            "name": "hackers",
            "id": 777,
        }
        MockLemmyClient.return_value = fake_client
        await command.callback(interaction, community_actor_url, forum_channel)

    MockLemmyClient.assert_called_once_with(f"https://{LEMMY_EXAMPLE_DOMAIN}")
    fake_client.resolve_community.assert_awaited_once_with(name="hackers")
    fedify_gateway.follow_community.assert_awaited_once_with(community_actor_url)
    database.remote_subscriptions.create_subscription.assert_called_once()
