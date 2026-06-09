"""Phase 5 scenario tests: inbound AP delivery onto shared group tables.

Each test exercises a concrete action in a defined system state and asserts
observable DB effects. All tests use a real SQLite DB and real CommunityRuntime.
Mock only outer boundaries: bot.fetch_forum_channel, bot.get_thread_by_id,
forum_channel.create_thread, thread.send, FedifyGatewayClient.
"""

from __future__ import annotations
from support.runtime import build_test_policy_service

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.community_sync.runtime import CommunityRuntime
from src.db import Database
from src.content_publish_service import ContentPublishService
from src.fedify_gateway_client import PublishContentResult
from src.activitypub_models import ActivityPubEvent
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN

COMMUNITY_ACTOR_URL = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"
POST_AP_ID = f"https://{LEMMY_EXAMPLE_DOMAIN}/post/1"
COMMENT_AP_ID = f"https://{LEMMY_EXAMPLE_DOMAIN}/comment/1"
COMMENT2_AP_ID = f"https://{LEMMY_EXAMPLE_DOMAIN}/comment/2"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite database for Phase 5 inbound AP scenario tests."""
    database = Database(f"sqlite:///{tmp_path / 'phase5.db'}")
    database.create_all()
    return database


def _accepted_subscription(database: Database, *, channel_id: int) -> None:
    """Insert one accepted community subscription for the shared hackers community."""
    database.remote_subscriptions.create_subscription(
        discord_channel_id=channel_id,
        lemmy_community_actor_id=COMMUNITY_ACTOR_URL,
        lemmy_community_name="hackers",
        lemmy_community_id=42,
        community_handle=f"!hackers@{LEMMY_EXAMPLE_DOMAIN}",
        community_inbox_url=f"{COMMUNITY_ACTOR_URL}/inbox",
        follow_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/activities/follow/{channel_id}",
        status="accepted",
    )


def _registered_user(database: Database) -> None:
    """Insert one registered local user actor for outbound publish scenarios."""
    actor_url = f"https://{BRIDGE_HOST_DOMAIN}/users/alice"
    database.users.create_user(
        discord_user_id="123",
        activitypub_username="alice",
        actor_url=actor_url,
        inbox_url=f"{actor_url}/inbox",
        outbox_url=f"{actor_url}/outbox",
        followers_url=f"{actor_url}/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )


def _publish_gateway() -> AsyncMock:
    """Build a mocked FedifyGatewayClient returning a valid PublishContentResult."""
    gateway = AsyncMock()
    gateway.publish_content.return_value = PublishContentResult(
        activity_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/comment/1",
        object_id=f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/1",
        community_actor_url=COMMUNITY_ACTOR_URL,
    )
    return gateway


def _publish_service(database: Database, gateway: AsyncMock) -> ContentPublishService:
    """Build a ContentPublishService wired to a fake gateway."""
    return ContentPublishService(
        database=database,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
            bridge_policy_service=build_test_policy_service(database),
)


def _community_runtime(database: Database, gateway: AsyncMock, *, bot: object) -> CommunityRuntime:
    """Build a real CommunityRuntime with a fake bot."""
    return CommunityRuntime(
        database=database,
        content_publish_service=_publish_service(database, gateway),
        discord_fanout=None,
        bot=bot,
    )


def _fake_post_event(
    *,
    ap_id: str = POST_AP_ID,
    community_actor_id: str = COMMUNITY_ACTOR_URL,
) -> ActivityPubEvent:
    """Build a fake inbound AP post event."""
    return ActivityPubEvent.model_validate({
        "event_type": "post.created",
        "delivery_id": "delivery-post-1",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "community_actor_id": community_actor_id,
        "actor_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/u/bob",
        "object": {
            "ap_id": ap_id,
            "kind": "post",
            "lemmy_id": 1,
            "title": "Test Post",
            "body_markdown": "Post body",
            "url": ap_id,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "author_name": "bob",
        },
    })


def _fake_comment_event(
    *,
    ap_id: str = COMMENT_AP_ID,
    post_ap_id: str = POST_AP_ID,
    parent_ap_id: str | None = None,
    community_actor_id: str = COMMUNITY_ACTOR_URL,
) -> ActivityPubEvent:
    """Build a fake inbound AP comment event."""
    return ActivityPubEvent.model_validate({
        "event_type": "comment.created",
        "delivery_id": f"delivery-comment-{ap_id}",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "community_actor_id": community_actor_id,
        "actor_id": f"https://{LEMMY_EXAMPLE_DOMAIN}/u/bob",
        "object": {
            "ap_id": ap_id,
            "kind": "comment",
            "lemmy_id": 10,
            "post_ap_id": post_ap_id,
            "post_lemmy_id": 1,
            "parent_ap_id": parent_ap_id,
            "body_markdown": "Comment body",
            "url": ap_id,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "author_name": "bob",
        },
    })


def _fake_bot(
    *,
    forum_channels: dict[int, object] | None = None,
    threads: dict[int, object] | None = None,
) -> SimpleNamespace:
    """Build a fake bot that returns pre-configured channels and threads."""

    async def fetch_forum_channel(channel_id: int) -> object:
        if forum_channels and channel_id in forum_channels:
            return forum_channels[channel_id]
        raise RuntimeError(f"No fake forum channel for id {channel_id}")

    async def get_thread_by_id(thread_id: int) -> object:
        if threads and thread_id in threads:
            return threads[thread_id]
        raise RuntimeError(f"No fake thread for id {thread_id}")

    async def wait_until_bridge_ready() -> None:
        pass

    return SimpleNamespace(
        fetch_forum_channel=fetch_forum_channel,
        get_thread_by_id=get_thread_by_id,
        wait_until_bridge_ready=wait_until_bridge_ready,
    )


def _fake_forum_channel(
    *,
    channel_id: int,
    thread_id: int,
    starter_message_id: int,
) -> SimpleNamespace:
    """Build a fake forum channel whose create_thread returns a (thread, message) tuple."""
    fake_thread = SimpleNamespace(id=thread_id)
    fake_message = SimpleNamespace(id=starter_message_id)
    fake_channel = SimpleNamespace(
        id=channel_id,
        create_thread=AsyncMock(return_value=(fake_thread, fake_message)),
    )
    return fake_channel


def _fake_thread(*, thread_id: int, sent_message_id: int) -> SimpleNamespace:
    """Build a fake Discord thread whose send() returns a message."""
    fake_sent = SimpleNamespace(id=sent_message_id)
    return SimpleNamespace(
        id=thread_id,
        send=AsyncMock(return_value=fake_sent),
    )


def _create_thread_group_for_inbound(
    database: Database,
    *,
    ap_object_id: str = POST_AP_ID,
    channel_id: int = 100,
    thread_id: int = 200,
    starter_message_id: int = 300,
) -> object:
    """Insert a CommunityThreadGroup with one inbound delivery row."""
    thread_group = database.discord_fanout_groups.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=None,
        source_thread_id=None,
        source_starter_message_id=None,
        ap_object_id=ap_object_id,
    )
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=channel_id,
        discord_thread_id=thread_id,
        discord_starter_message_id=starter_message_id,
        role="inbound",
    )
    return thread_group


# ---------------------------------------------------------------------------
# Phase 5 scenario tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase5_inbound_post_creates_thread_group_and_deliveries(
    tmp_path: Path,
) -> None:
    """Inbound AP post creates one CommunityThreadGroup and two inbound deliveries.

    System state: two accepted subscriptions (channels 100 and 101).
    bot.fetch_forum_channel returns pre-configured fake channels.
    Action: handle_inbound_post for a post event for community C.
    Assert: one CommunityThreadGroup with ap_object_id = post AP ID,
    two CommunityThreadGroupDelivery rows (role='inbound', one per channel),
    no PostLink rows written.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _accepted_subscription(database, channel_id=101)

    forum_ch_100 = _fake_forum_channel(channel_id=100, thread_id=200, starter_message_id=300)
    forum_ch_101 = _fake_forum_channel(channel_id=101, thread_id=500, starter_message_id=501)
    bot = _fake_bot(forum_channels={100: forum_ch_100, 101: forum_ch_101})

    runtime = _community_runtime(database, _publish_gateway(), bot=bot)
    event = _fake_post_event()

    from src.runtime import Runtime
    fake_runtime = SimpleNamespace(
        database=database,
        bot=bot,
        community_runtime=runtime,
    )

    result = await runtime.handle_inbound_post(event, fake_runtime)

    assert result.status == "processed"
    thread_group = database.discord_fanout_groups.get_thread_group_by_ap_object(POST_AP_ID)
    assert thread_group is not None
    assert thread_group.ap_object_id == POST_AP_ID
    assert thread_group.source_thread_id is None

    deliveries = database.discord_fanout_groups.get_thread_deliveries(thread_group.id)
    assert len(deliveries) == 2
    assert all(d.role == "inbound" for d in deliveries)
    thread_ids = {d.discord_thread_id for d in deliveries}
    assert thread_ids == {200, 500}
    # Remote post starters must use the same full fediverse handle as comments
    # so authors are not ambiguous across remote instances.
    expected_content = (
        "**Test Post**\n\n"
        f"Author: `bob@{LEMMY_EXAMPLE_DOMAIN}`\n\n"
        "Post body\n\n"
        f"https://{LEMMY_EXAMPLE_DOMAIN}/post/1"
    )
    forum_ch_100.create_thread.assert_any_await(name="Test Post", content=expected_content)
    forum_ch_101.create_thread.assert_any_await(name="Test Post", content=expected_content)

    # No PostLink rows must have been written.
    from src.models import PostLink
    from sqlalchemy import select
    with database.session() as session:
        post_links = list(session.scalars(select(PostLink)))
    assert len(post_links) == 0


@pytest.mark.asyncio
async def test_phase5_inbound_post_dedup_skips_on_existing_thread_group(
    tmp_path: Path,
) -> None:
    """Inbound AP post with an existing thread group is silently skipped.

    System state: one accepted subscription, CommunityThreadGroup already exists
    for the post AP ID.
    Action: handle_inbound_post with the same post event.
    Assert: result status='skipped', no new thread group or delivery rows,
    forum_channel.create_thread not called.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)

    # Pre-insert thread group for this AP object.
    _create_thread_group_for_inbound(database, ap_object_id=POST_AP_ID)

    forum_ch = _fake_forum_channel(channel_id=100, thread_id=999, starter_message_id=998)
    bot = _fake_bot(forum_channels={100: forum_ch})
    runtime = _community_runtime(database, _publish_gateway(), bot=bot)
    fake_runtime = SimpleNamespace(database=database, bot=bot, community_runtime=runtime)

    event = _fake_post_event()
    result = await runtime.handle_inbound_post(event, fake_runtime)

    assert result.status == "skipped"
    # Only the pre-existing delivery row — no new rows.
    thread_group = database.discord_fanout_groups.get_thread_group_by_ap_object(POST_AP_ID)
    deliveries = database.discord_fanout_groups.get_thread_deliveries(thread_group.id)
    assert len(deliveries) == 1
    forum_ch.create_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_phase5_inbound_comment_root_creates_message_group_flat(
    tmp_path: Path,
) -> None:
    """Root inbound comment (direct reply to post) delivered flat into all threads.

    System state: one accepted subscription, CommunityThreadGroup for the post AP ID
    with one inbound delivery (thread 200). AP comment has parent_ap_id=None.
    Action: handle_inbound_comment.
    Assert: one CommunityMessageGroup with parent_message_group_id=None,
    one CommunityMessageGroupDelivery (thread 200, role='inbound'),
    thread.send called with reference=None, no CommentLink rows written.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _create_thread_group_for_inbound(
        database, ap_object_id=POST_AP_ID, channel_id=100, thread_id=200,
    )

    fake_thread = _fake_thread(thread_id=200, sent_message_id=400)
    bot = _fake_bot(threads={200: fake_thread})
    runtime = _community_runtime(database, _publish_gateway(), bot=bot)
    fake_runtime = SimpleNamespace(database=database, bot=bot, community_runtime=runtime)

    # parent_ap_id=None → root comment, should send flat.
    event = _fake_comment_event(parent_ap_id=None)
    result = await runtime.handle_inbound_comment(event, fake_runtime)

    assert result.status == "processed"
    message_group = database.discord_fanout_groups.get_message_group_by_ap_object(COMMENT_AP_ID)
    assert message_group is not None
    assert message_group.parent_message_group_id is None

    deliveries = database.discord_fanout_groups.get_message_deliveries(message_group.id)
    assert len(deliveries) == 1
    assert deliveries[0].discord_thread_id == 200
    assert deliveries[0].role == "inbound"

    # Flat send: reference must be None.
    fake_thread.send.assert_awaited_once()
    _, kwargs = fake_thread.send.call_args
    assert kwargs.get("reference") is None

    # No CommentLink rows written.
    from src.models import CommentLink
    from sqlalchemy import select
    with database.session() as session:
        comment_links = list(session.scalars(select(CommentLink)))
    assert len(comment_links) == 0


@pytest.mark.asyncio
async def test_phase5_inbound_comment_reply_chain_resolves_to_prior_delivery(
    tmp_path: Path,
) -> None:
    """Inbound comment reply resolves to the correct per-thread parent message.

    System state: two subscriptions, CommunityThreadGroup with inbound deliveries
    for threads 200 and 500. Prior CommunityMessageGroup M with deliveries:
    (thread 200, msg 300) and (thread 500, msg 400). AP comment has
    parent_ap_id = M's ap_object_id.
    Action: handle_inbound_comment.
    Assert: new CommunityMessageGroup.parent_message_group_id = M.id,
    thread 200 send called with reference.message_id=300,
    thread 500 send called with reference.message_id=400.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _accepted_subscription(database, channel_id=101)

    thread_group = database.discord_fanout_groups.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=None,
        source_thread_id=None,
        source_starter_message_id=None,
        ap_object_id=POST_AP_ID,
    )
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100,
        discord_thread_id=200,
        discord_starter_message_id=201,
        role="inbound",
    )
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=101,
        discord_thread_id=500,
        discord_starter_message_id=501,
        role="inbound",
    )

    # Prior message group M (parent comment that was already delivered).
    prior_group = database.discord_fanout_groups.create_message_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        thread_group_id=thread_group.id,
        source_channel_id=None,
        source_thread_id=None,
        source_message_id=None,
        ap_object_id=COMMENT_AP_ID,
    )
    database.discord_fanout_groups.add_message_delivery(
        message_group_id=prior_group.id,
        discord_channel_id=100,
        discord_thread_id=200,
        discord_message_id=300,
        role="inbound",
    )
    database.discord_fanout_groups.add_message_delivery(
        message_group_id=prior_group.id,
        discord_channel_id=101,
        discord_thread_id=500,
        discord_message_id=400,
        role="inbound",
    )

    fake_thread_200 = _fake_thread(thread_id=200, sent_message_id=700)
    fake_thread_500 = _fake_thread(thread_id=500, sent_message_id=800)
    bot = _fake_bot(threads={200: fake_thread_200, 500: fake_thread_500})
    runtime = _community_runtime(database, _publish_gateway(), bot=bot)
    fake_runtime = SimpleNamespace(database=database, bot=bot, community_runtime=runtime)

    # Reply to prior comment M.
    event = _fake_comment_event(ap_id=COMMENT2_AP_ID, parent_ap_id=COMMENT_AP_ID)
    result = await runtime.handle_inbound_comment(event, fake_runtime)

    assert result.status == "processed"
    new_group = database.discord_fanout_groups.get_message_group_by_ap_object(COMMENT2_AP_ID)
    assert new_group is not None
    assert new_group.parent_message_group_id == prior_group.id

    # Thread 200 must reference msg 300; thread 500 must reference msg 400.
    fake_thread_200.send.assert_awaited_once()
    _, kwargs_200 = fake_thread_200.send.call_args
    assert kwargs_200.get("reference") is not None
    assert kwargs_200["reference"].message_id == 300

    fake_thread_500.send.assert_awaited_once()
    _, kwargs_500 = fake_thread_500.send.call_args
    assert kwargs_500.get("reference") is not None
    assert kwargs_500["reference"].message_id == 400


@pytest.mark.asyncio
async def test_phase5_inbound_comment_dedup_skips_on_existing_message_group(
    tmp_path: Path,
) -> None:
    """Inbound comment with existing message group is silently skipped.

    System state: CommunityMessageGroup already exists for the comment AP ID.
    Action: handle_inbound_comment with same event.
    Assert: result status='skipped', no new rows, thread.send not called.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)

    thread_group = _create_thread_group_for_inbound(
        database, ap_object_id=POST_AP_ID, channel_id=100, thread_id=200,
    )

    # Pre-insert message group for this comment AP ID.
    database.discord_fanout_groups.create_message_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        thread_group_id=thread_group.id,
        source_channel_id=None,
        source_thread_id=None,
        source_message_id=None,
        ap_object_id=COMMENT_AP_ID,
    )

    fake_thread = _fake_thread(thread_id=200, sent_message_id=999)
    bot = _fake_bot(threads={200: fake_thread})
    runtime = _community_runtime(database, _publish_gateway(), bot=bot)
    fake_runtime = SimpleNamespace(database=database, bot=bot, community_runtime=runtime)

    event = _fake_comment_event()
    result = await runtime.handle_inbound_comment(event, fake_runtime)

    assert result.status == "skipped"
    fake_thread.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_phase5_outbound_publish_does_not_create_post_link(
    tmp_path: Path,
) -> None:
    """Outbound Discord message publish no longer writes PostLink or CommentLink rows.

    System state: one accepted subscription, one registered user, CommunityThreadGroup
    for thread 200 (with source delivery).
    Action: handle_discord_message for message 400.
    Assert: result status='published', no PostLink rows, no CommentLink rows,
    CommunityMessageGroup exists, gateway called once.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _registered_user(database)

    ap_object_id = f"https://{BRIDGE_HOST_DOMAIN}/objects/post/1"
    ap_activity_id = f"https://{BRIDGE_HOST_DOMAIN}/activities/create/post/1"
    thread_group = database.discord_fanout_groups.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=100,
        source_thread_id=200,
        source_starter_message_id=300,
        ap_activity_id=ap_activity_id,
        ap_object_id=ap_object_id,
    )
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=100,
        discord_thread_id=200,
        discord_starter_message_id=300,
        role="source",
    )

    gateway = _publish_gateway()
    bot = _fake_bot()
    runtime = _community_runtime(database, gateway, bot=bot)

    from types import SimpleNamespace as NS
    channel = NS(id=200, parent_id=100)
    author = NS(id=123, display_name="Alice", name="alice")
    message = NS(
        id=400,
        content="hello",
        author=author,
        channel=channel,
        reference=None,
    )

    result = await runtime.handle_discord_message(message=message)

    assert result.status == "published"
    message_group = database.discord_fanout_groups.get_message_group_by_source_message(400)
    assert message_group is not None
    gateway.publish_content.assert_awaited_once()

    # No PostLink or CommentLink rows must exist.
    from src.models import PostLink, CommentLink
    from sqlalchemy import select
    with database.session() as session:
        post_links = list(session.scalars(select(PostLink)))
        comment_links = list(session.scalars(select(CommentLink)))
    assert len(post_links) == 0
    assert len(comment_links) == 0
