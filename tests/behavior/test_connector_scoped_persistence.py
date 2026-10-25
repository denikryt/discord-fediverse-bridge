
from support.community_events import create_thread, configure_renderer, ScenarioDiscord, physical_forum
"""Real runtime actions with mixed connector claims and retained physical copies."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.db import Database
from src.db.repositories.platform_surfaces import PlatformSurfaceRepository
from src.models import ChannelCommunitySubscription, PlatformConnector, ChannelSurface
from support.activitypub import build_post_created_event, build_comment_created_event
from support.discord import build_forum_channel_tuple_result, build_bot
from support.gateway import build_gateway_mock
from support.runtime import build_community_runtime, build_test_policy_service


@pytest.mark.asyncio
@pytest.mark.parametrize("discord_present", [True, False])
async def test_inbound_post_with_fluxer_claim_routes_only_actual_discord_targets(
    tmp_path, discord_present
):
    """One AP post is retained/delivered only when a real Discord claim is eligible."""
    database = Database(f"sqlite:///{tmp_path / 'mixed.db'}")
    database.migrate()
    actor = "https://remote.test/c/news"
    if discord_present:
        database.remote_subscriptions.create_subscription(
            discord_channel_id=900,
            discord_guild_id=1,
            lemmy_community_actor_id=actor,
            lemmy_community_name="news",
            lemmy_community_id=None,
            status="accepted",
        )
    with database.immediate_session() as session:
        connector = PlatformSurfaceRepository.add_connector(
            session,
            connector_key="fluxer-a",
            platform="fluxer",
            instance_origin="https://fluxer.test",
        )
        surface = PlatformSurfaceRepository.ensure_channel(
            session,
            connector_key=connector.connector_key,
            external_channel_id="900",
            kind="forum",
        )
        database.remote_subscriptions.add_subscription(
            session, surface.id, lemmy_community_actor_id=actor, status="accepted"
        )
    forum = build_forum_channel_tuple_result(
        channel_id=900, thread_id=111, starter_message_id=222
    )
    thread = SimpleNamespace(
        id=111, send=AsyncMock(return_value=SimpleNamespace(id=333))
    )
    bot = build_bot(forum_channels={900: forum}, threads={111: thread})
    calls = []
    original = bot.fetch_forum_channel

    async def fetch(channel_id):
        """Capture only the Discord SDK edge; persistence and routing remain real."""
        assert type(channel_id) is int
        calls.append(channel_id)
        return await original(channel_id)

    bot.fetch_forum_channel = fetch
    community_runtime = build_community_runtime(
        database, fedify_gateway=build_gateway_mock(), bot=bot
    )
    runtime = SimpleNamespace(
        database=database,
        bot=bot,
        community_runtime=community_runtime,
        discord_adapter=ScenarioDiscord(community_runtime),
        bridge_policy_service=build_test_policy_service(database),
    )
    post = build_post_created_event(
        object_id="https://remote.test/post/1", community_actor_id=actor
    )
    result = await community_runtime.handle_inbound_post(post, runtime)
    group = database.discord_fanout_groups.get_thread_group_by_ap_object(
        post.object.ap_id
    )
    if discord_present:
        assert result.status == "processed" and group is not None
        assert calls == [900]
        deliveries = database.discord_fanout_groups.get_thread_deliveries(group.id)
        assert len(deliveries) == 1 and deliveries[0].external_thread_id == "111"
        assert (
            group.source_connector_id is None
            and group.source_channel_surface_id is None
        )
        comment = build_comment_created_event(
            object_id="https://remote.test/comment/1",
            post_ap_id=post.object.ap_id,
            community_actor_id=actor,
        )
        comment_result = await community_runtime.handle_inbound_comment(
            comment, runtime
        )
        assert comment_result.status == "processed"
        message_group = database.discord_fanout_groups.get_message_group_by_ap_object(
            comment.object.ap_id
        )
        assert (
            len(database.discord_fanout_groups.get_message_deliveries(message_group.id))
            == 1
        )
        assert calls == [900]
        assert (
            await community_runtime.handle_inbound_post(post, runtime)
        ).status == "skipped"
        assert (
            await community_runtime.handle_inbound_comment(comment, runtime)
        ).status == "skipped"
    else:
        assert result.status == "skipped" and group is None and calls == []
        assert (
            await community_runtime.handle_inbound_post(post, runtime)
        ).status == "skipped"
    assert database.remote_subscriptions.count_subscriptions_for_community(actor) == (
        2 if discord_present else 1
    )
    with database.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    database.close()


def _fluxer_channel(session, external_id, kind="forum"):
    """Seed identity metadata only; synthetic fixtures never contact Fluxer."""
    connector = session.scalar(
        select(PlatformConnector).where(PlatformConnector.connector_key == "fluxer-a")
    )
    if connector is None:
        connector = PlatformSurfaceRepository.add_connector(
            session,
            connector_key="fluxer-a",
            platform="fluxer",
            instance_origin="https://fluxer.test",
        )
    return PlatformSurfaceRepository.ensure_channel(
        session,
        connector_key=connector.connector_key,
        external_channel_id=external_id,
        external_guild_id="10",
        kind=kind,
    )


def _community(database):
    """Create a real local actor with a Discord host claim."""
    from src.local_communities.service import LocalCommunityService

    LocalCommunityService(
        database=database,
        base_url="https://bridge.example",
        keypair_generator=lambda: ("public", "private"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="hackers",
        name="Hackers",
        description="Local community",
        created_by_discord_user_id="123",
    )
    return database.local_communities.get_local_community_by_slug("hackers")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "accept_mode", ["explicit", "implicit_post", "implicit_comment"]
)
async def test_mixed_pending_accept_promotes_globally_and_notifies_only_discord(
    tmp_path, accept_mode
):
    """A shared Accept activates both owners; only the Discord initiator gets a DM."""
    from datetime import UTC, datetime
    from src.activitypub_handlers import dispatch_activitypub_event
    from src.activitypub_models import FollowLifecycleEvent

    database = Database(f"sqlite:///{tmp_path / 'accept.db'}")
    database.migrate()
    actor = "https://remote.test/c/news"
    follow_id = "https://bridge.example/actors/services/bridge/activities/follow/news"
    follow = database.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id=actor,
        follow_activity_id=follow_id,
        community_inbox_url=actor + "/inbox",
        status="pending",
    )
    discord = database.remote_subscriptions.create_subscription(
        discord_channel_id=900,
        discord_guild_id=1,
        lemmy_community_actor_id=actor,
        lemmy_community_name="news",
        lemmy_community_id=None,
        initiated_by_discord_user_id="123",
        status="pending",
    )
    with database.immediate_session() as session:
        surface = _fluxer_channel(session, "900")
        fluxer = database.remote_subscriptions.add_subscription(
            session,
            surface.id,
            lemmy_community_actor_id=actor,
            initiated_by_discord_user_id="456",
            status="pending",
        )
        fluxer_id, surface_id = fluxer.id, surface.id
    dm = SimpleNamespace(send=AsyncMock())
    forum = build_forum_channel_tuple_result(
        channel_id=900, thread_id=111, starter_message_id=222
    )
    thread = SimpleNamespace(
        id=111, send=AsyncMock(return_value=SimpleNamespace(id=333))
    )
    bot = build_bot(forum_channels={900: forum}, threads={111: thread})
    bot.fetch_user = AsyncMock(return_value=dm)
    runtime = SimpleNamespace(
        database=database,
        bot=bot,
        community_runtime=build_community_runtime(
            database, fedify_gateway=build_gateway_mock(), bot=bot
        ),
        bridge_policy_service=build_test_policy_service(database),
    )
    runtime.discord_adapter = ScenarioDiscord(runtime.community_runtime)
    post = build_post_created_event(
        object_id="https://remote.test/post/accept", community_actor_id=actor
    )
    if accept_mode == "explicit":
        event = FollowLifecycleEvent(
            event_type="follow.accepted",
            delivery_id="https://remote.test/accept/1",
            occurred_at=datetime.now(UTC),
            community_actor_id=actor,
            actor_id=actor,
            object={"follow_activity_id": follow_id},
        )
    elif accept_mode == "implicit_post":
        event = post
    else:
        # Give the real comment handler a retained root instead of mocking routing.
        group = database.discord_fanout_groups.create_thread_group(
            community_actor_id=actor,
            source_channel_id=None,
            source_thread_id=None,
            source_starter_message_id=None,
            ap_object_id=post.object.ap_id,
        )
        database.discord_fanout_groups.add_thread_delivery(
            thread_group_id=group.id,
            discord_channel_id=900,
            discord_thread_id=111,
            discord_starter_message_id=222,
            role="inbound",
        )
        event = build_comment_created_event(
            object_id="https://remote.test/comment/accept",
            post_ap_id=post.object.ap_id,
            community_actor_id=actor,
        )
    result = await dispatch_activitypub_event(event, runtime)
    assert result.status == "processed"
    with database.session() as session:
        assert (
            session.get(ChannelCommunitySubscription, discord.id).status == "accepted"
        )
        assert session.get(ChannelCommunitySubscription, fluxer_id).status == "accepted"
        assert (
            session.get(ChannelSurface, surface_id).remote_subscription_id == fluxer_id
        )
    actual_follow = database.bridge_actor_follows.get_bridge_actor_follow(actor)
    assert actual_follow.id == follow.id and actual_follow.status == "accepted"
    assert database.remote_subscriptions.count_subscriptions_for_community(actor) == 2
    bot.fetch_user.assert_awaited_once_with(123)
    assert "<#900>" in dm.send.await_args.args[0]
    assert "<#None>" not in dm.send.await_args.args[0]
    await dispatch_activitypub_event(event, runtime)
    assert dm.send.await_count == 1
    if accept_mode == "implicit_post":
        assert (
            len(
                database.discord_fanout_groups.get_thread_deliveries(
                    database.discord_fanout_groups.get_thread_group_by_ap_object(
                        post.object.ap_id
                    ).id
                )
            )
            == 1
        )
    elif accept_mode == "implicit_comment":
        assert (
            len(
                database.discord_fanout_groups.get_message_deliveries(
                    database.discord_fanout_groups.get_message_group_by_ap_object(
                        event.object.ap_id
                    ).id
                )
            )
            == 1
        )
    database.close()


@pytest.mark.asyncio
async def test_local_publish_with_mixed_subscribers_creates_only_discord_copies(
    tmp_path,
):
    """A subscriber-origin post reaches host and sibling, with one AP publication."""
    from src.content_publish_service import ContentPublishService
    from src.local_communities.runtime import LocalCommunityRuntime
    from src.fedify_gateway_client import PublishLocalCommunityContentResult
    from support.db import add_registered_user
    from support.discord import (
        build_forum_channel_object_result,
        build_thread,
        build_starter_message,
    )

    database = Database(f"sqlite:///{tmp_path / 'local.db'}")
    database.migrate()
    community = _community(database)
    for channel in (200, 300):
        database.local_subscribers.create_local_subscriber(
            local_community_id=community.id,
            discord_channel_id=channel,
            discord_guild_id=10,
            initiated_by_discord_user_id="123",
            status="active",
        )
    with database.immediate_session() as session:
        surface = _fluxer_channel(session, "300")
        fluxer = database.local_subscribers.add_local_subscriber(
            session,
            surface.id,
            local_community_id=community.id,
            status="active",
        )
        fluxer_id = fluxer.id
    add_registered_user(database)
    gateway = build_gateway_mock()
    configure_renderer(gateway, database)
    gateway.publish_local_community_content = AsyncMock(
        return_value=PublishLocalCommunityContentResult(
            "https://bridge.example/actors/communities/hackers/activities/create/1",
            "https://bridge.example/actors/communities/hackers/objects/1",
            community.actor_url,
            0,
            0,
        )
    )
    policy = build_test_policy_service(database)
    bot = build_bot(
        forum_channels={
            100: build_forum_channel_object_result(
                channel_id=100, thread_id=1100, starter_message_id=1200
            ),
            300: build_forum_channel_object_result(
                channel_id=300, thread_id=3300, starter_message_id=3400
            ),
        }
    )
    runtime = LocalCommunityRuntime(
        database=database,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
        bot=bot,
        bridge_policy_service=policy,
        content_publish_service=ContentPublishService(
            database=database,
            fedify_gateway=gateway,
            bridge_prefix="[bridge]",
            bridge_policy_service=policy,
        ),
    )
    result = await create_thread(runtime, thread=build_thread(thread_id=2200, channel_id=200), starter_message=build_starter_message(message_id=2300))
    assert result.status == "published"
    row = database.local_community_content.get_local_community_thread_by_ap_object_id(
        result.object_id
    )
    surfaces = database.local_community_surfaces.list_local_community_thread_surfaces(
        row.id
    )
    assert {physical_forum(database, s) for s in surfaces} == {100, 200, 300}
    assert all(s.local_subscriber_id != fluxer_id for s in surfaces)
    assert len(surfaces) == 3
    assert database.local_subscribers.count_discord_local_subscribers(community.id) == 2
    gateway.render_community.assert_awaited_once()
    database.close()


def test_dashboard_mixed_owners_preserves_discord_cards_and_global_follow(tmp_path):
    """HTTP dashboard excludes Fluxer placements while retaining global AP Follow."""
    from fastapi.testclient import TestClient
    from src.http_api import create_http_app
    from src.dashboard import DEFAULT_DASHBOARD_PAGE_TITLE

    database = Database(f"sqlite:///{tmp_path / 'dashboard-mixed.db'}")
    database.migrate()
    community = _community(database)
    database.discord_directory.upsert_guild_snapshot(
        discord_guild_id=10, guild_name="Discord guild"
    )
    for channel in (100, 200, 900):
        database.discord_directory.upsert_channel_snapshot(
            discord_channel_id=channel,
            discord_guild_id=10,
            channel_name=f"discord-{channel}",
            channel_type="forum",
        )
    database.local_subscribers.create_local_subscriber(
        local_community_id=community.id,
        discord_channel_id=200,
        discord_guild_id=10,
        initiated_by_discord_user_id="123",
        status="active",
    )
    actor = "https://remote.test/c/news"
    database.remote_subscriptions.create_subscription(
        discord_channel_id=900,
        discord_guild_id=10,
        lemmy_community_actor_id=actor,
        lemmy_community_name="news",
        lemmy_community_id=None,
        status="accepted",
    )
    database.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id=actor,
        follow_activity_id="https://bridge.example/actors/services/bridge/activities/follow/news",
        community_inbox_url=actor + "/inbox",
        status="accepted",
    )
    settings = SimpleNamespace(
        fedify_origin="https://bridge.example",
        normalized_fedify_origin="https://bridge.example",
        fedify_actor_identifier="bridge",
        dashboard_page_title=DEFAULT_DASHBOARD_PAGE_TITLE,
        federation_allowlist=[],
        fedify_shared_secret="secret",
        registration_session_cookie_name="session",
        registration_session_ttl_seconds=3600,
    )
    runtime = SimpleNamespace(
        database=database,
        settings=settings,
        registration_service=SimpleNamespace(),
        discord_oauth_client=SimpleNamespace(),
        fedify_gateway=SimpleNamespace(),
        bot=SimpleNamespace(),
        bridge_policy_service=build_test_policy_service(database, settings),
    )
    with TestClient(create_http_app(runtime)) as client:
        before = client.get("/dashboard/data").json()
        with database.immediate_session() as session:
            subscriber_surface = _fluxer_channel(session, "200")
            database.local_subscribers.add_local_subscriber(
                session,
                subscriber_surface.id,
                local_community_id=community.id,
                status="active",
            )
            remote_surface = _fluxer_channel(session, "900")
            database.remote_subscriptions.add_subscription(
                session,
                remote_surface.id,
                lemmy_community_actor_id=actor,
                lemmy_community_name="fluxer-news",
                status="accepted",
            )
        response = client.get("/dashboard/data")
        assert response.status_code == 200
        after = response.json()
        assert after["discordGuilds"] == before["discordGuilds"]
        assert after["localCommunities"] == before["localCommunities"]
        assert after["localCommunities"][0]["localSubscriberCount"] == 1
        assert (
            after["localCommunities"][0]["hostDiscord"]["forumChannelName"]
            == "discord-100"
        )
        assert after["bridgeActorFollows"] == before["bridgeActorFollows"]
        assert after["bridgeActorFollows"][0]["communityActorUrl"] == actor
    assert database.local_subscribers.count_local_subscribers(community.id) == 2
    assert database.remote_subscriptions.count_subscriptions_for_community(actor) == 2
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("guild_id", [10, None])
async def test_list_subscriptions_mixed_owners_only_renders_discord(
    tmp_path, command_tree, interaction, guild_id
):
    """Real listing operations exclude Fluxer in guild and guildless contexts."""
    from discordops import run_operation_definition_async
    from src.operations import ListSubscriptionsInput, list_subscriptions_operation
    from src.commands import list_subs
    from support.db import add_registered_user

    database = Database(f"sqlite:///{tmp_path / 'list-mixed.db'}")
    database.migrate()
    community = _community(database)
    add_registered_user(database, discord_user_id=str(interaction.user.id))
    database.local_subscribers.create_local_subscriber(
        local_community_id=community.id,
        discord_channel_id=200,
        discord_guild_id=10,
        initiated_by_discord_user_id="123",
        status="active",
    )
    database.remote_subscriptions.create_subscription(
        discord_channel_id=900,
        discord_guild_id=10,
        lemmy_community_actor_id="https://remote.test/c/news",
        lemmy_community_name="news",
        lemmy_community_id=None,
        status="accepted",
    )
    with database.immediate_session() as session:
        surface = _fluxer_channel(session, "900")
        database.remote_subscriptions.add_subscription(
            session,
            surface.id,
            lemmy_community_actor_id="https://remote.test/c/fluxer",
            lemmy_community_name="fluxer-only",
            status="accepted",
        )
        local_surface = _fluxer_channel(session, "200")
        database.local_subscribers.add_local_subscriber(
            session,
            local_surface.id,
            local_community_id=community.id,
            status="active",
        )
    result = await run_operation_definition_async(
        list_subscriptions_operation,
        ListSubscriptionsInput(database=database, guild_id=guild_id),
    )
    assert result.applied
    assert [
        r.discord_channel_id for r in result.extra_kwargs["remote_subscriptions"]
    ] == [900]
    assert [r.discord_channel_id for r in result.extra_kwargs["local_subscribers"]] == (
        [200] if guild_id else []
    )
    # Guildless invocation uses the operation contract: the slash command's
    # established access policy rejects DMs before it performs any listing.
    settings = SimpleNamespace(discord_guild_allowlist=[])
    list_subs.register(
        command_tree, database, settings, build_test_policy_service(database, settings)
    )
    interaction.guild_id = guild_id
    await command_tree.commands["list-subscriptions"].callback(interaction)
    if guild_id:
        embed = interaction.response.send_message.await_args.kwargs["embed"]
        assert "<#900>" in embed.description and "<#200>" in embed.description
        assert (
            "<#None>" not in embed.description
            and "fluxer-only" not in embed.description
        )
    else:
        assert "embed" not in interaction.response.send_message.await_args.kwargs
    database.close()


def test_personal_feed_discord_actions_cannot_retarget_fluxer_binding(tmp_path):
    """Views and mutations protect a non-Discord binding alongside a real feed."""
    from src.models import PersonalFeedBinding
    from src.personal_feeds import PersonalFeedService
    from support.db import add_registered_user

    database = Database(f"sqlite:///{tmp_path / 'feeds-mixed.db'}")
    database.migrate()
    add_registered_user(database, discord_user_id="123", username="alice")
    add_registered_user(database, discord_user_id="456", username="bob")
    service = PersonalFeedService(database)
    assert service.bind_or_move("123", 10, 200).status == "bound"
    bob = database.users.get_user_by_discord_user_id("456")
    with database.immediate_session() as session:
        surface = _fluxer_channel(session, "200", "text")
        binding = PersonalFeedBinding(user_id=bob.id)
        session.add(binding)
        session.flush()
        PlatformSurfaceRepository.claim_personal_feed(session, surface.id, binding.id)
        binding_id, surface_id = binding.id, surface.id
    assert service.get_by_discord_user_id("123").discord_channel_id == 200
    assert service.resolve_root_binding(10, 200).discord_user_id == "123"
    assert database.personal_feed_bindings.get_by_discord_user_id("456") is None
    assert service.get_by_discord_user_id("456") is None
    assert service.resolve_root_binding(10, 201) is None
    assert not service.is_active_personal_feed_channel(bob.id, 200)
    assert not database.personal_feed_bindings.is_current_channel_for_user(bob.id, 200)
    assert service.is_active_personal_feed_channel(
        database.users.get_user_by_discord_user_id("123").id, 200
    )
    assert service.unbind("456").status == "not_bound"
    for expected in (None, binding_id):
        assert (
            service.bind_or_move("456", 20, 201, expected_binding_id=expected).status
            == "binding_changed"
        )
    with database.session() as session:
        retained = session.get(PersonalFeedBinding, binding_id)
        surface = session.get(ChannelSurface, surface_id)
        assert retained.user_id == bob.id
        assert retained.discord_channel_id is None and retained.discord_guild_id is None
        assert (
            surface.personal_feed_binding_id == binding_id
            and surface.role == "personal_feed"
        )
    # The same service remains usable for the existing Discord owner.
    assert service.bind_or_move("123", 10, 201).status == "moved"
    assert service.resolve_root_binding(10, 201).discord_user_id == "123"
    assert service.unbind("123").status == "unbound"
    database.close()
