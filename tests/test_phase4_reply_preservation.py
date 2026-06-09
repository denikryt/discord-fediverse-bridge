"""Phase 4 scenario tests: reply preservation across mirrored threads.

Each test exercises a concrete action in a defined system state and asserts
observable DB effects and Discord send call arguments. All tests use a real
SQLite DB, real CommunityRuntime, and real ContentPublishService. Mock only
outer boundaries: FedifyGatewayClient, bot.get_thread_by_id, and thread.send.

The send mock captures the `reference` kwarg so tests can assert the correct
Discord MessageReference is forwarded to the mirror thread.
"""

from __future__ import annotations
from support.runtime import build_test_policy_service

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from src.community_sync.discord_fanout import DiscordFanout
from src.community_sync.runtime import CommunityRuntime
from src.db import Database
from src.content_publish_service import ContentPublishService
from src.fedify_gateway_client import PublishContentResult
from tests_constants import BRIDGE_HOST_DOMAIN, LEMMY_EXAMPLE_DOMAIN

COMMUNITY_ACTOR_URL = f"https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers"


# ---------------------------------------------------------------------------
# Shared helpers (mirror Phase 3 test structure)
# ---------------------------------------------------------------------------


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite database for Phase 4 reply-preservation tests."""
    database = Database(f"sqlite:///{tmp_path / 'phase4-reply.db'}")
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


def _publish_gateway(
    *,
    activity_id: str | None = None,
    object_id: str | None = None,
) -> AsyncMock:
    """Build a mocked FedifyGatewayClient that returns a valid PublishContentResult."""
    gateway = AsyncMock()
    gateway.publish_content.return_value = PublishContentResult(
        activity_id=activity_id or f"https://{BRIDGE_HOST_DOMAIN}/users/alice/activities/create/comment/1",
        object_id=object_id or f"https://{BRIDGE_HOST_DOMAIN}/users/alice/objects/comment/1",
        community_actor_url=COMMUNITY_ACTOR_URL,
    )
    return gateway


def _publish_service(database: Database, gateway: AsyncMock) -> ContentPublishService:
    """Build a ContentPublishService wired to one fake gateway boundary."""
    return ContentPublishService(
        database=database,
        fedify_gateway=gateway,
        bridge_prefix="[bridge]",
            bridge_policy_service=build_test_policy_service(database),
)


def _community_runtime(
    database: Database,
    gateway: AsyncMock,
    *,
    discord_fanout: DiscordFanout | None = None,
) -> CommunityRuntime:
    """Build a real CommunityRuntime with optional DiscordFanout."""
    return CommunityRuntime(
        database=database,
        content_publish_service=_publish_service(database, gateway),
        discord_fanout=discord_fanout,
    )


def _fake_message(
    *,
    message_id: int = 400,
    thread_id: int = 200,
    channel_id: int = 100,
    author_id: int = 123,
    reference_message_id: int | None = None,
) -> SimpleNamespace:
    """Return one fake Discord thread message with the minimal attributes required.

    When reference_message_id is set the message simulates a Discord reply:
    message.reference.message_id will be the given value.
    """
    channel = SimpleNamespace(id=thread_id, parent_id=channel_id)
    author = SimpleNamespace(id=author_id, display_name="Alice", name="alice")
    reference = (
        SimpleNamespace(message_id=reference_message_id)
        if reference_message_id is not None
        else None
    )
    return SimpleNamespace(
        id=message_id,
        content="hello from inside the thread",
        author=author,
        channel=channel,
        reference=reference,
    )


def _create_thread_group_with_source_delivery(
    database: Database,
    *,
    channel_id: int = 100,
    thread_id: int = 200,
    starter_message_id: int = 300,
) -> object:
    """Insert a CommunityThreadGroup plus its source delivery row.

    Also inserts a PostLink so publish_thread_message can resolve AP context
    via the legacy path (same pattern as Phase 3 helper).
    """
    ap_object_id = f"https://{BRIDGE_HOST_DOMAIN}/objects/post/1"
    ap_activity_id = f"https://{BRIDGE_HOST_DOMAIN}/activities/create/post/1"
    database.legacy_lemmy_mappings.create_post_link(
        lemmy_post_id=-thread_id,
        lemmy_post_ap_id=ap_object_id,
        discord_forum_channel_id=channel_id,
        discord_forum_thread_id=thread_id,
        discord_starter_message_id=starter_message_id,
        direction="discord_to_activitypub",
    )
    thread_group = database.discord_fanout_groups.create_thread_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        source_channel_id=channel_id,
        source_thread_id=thread_id,
        source_starter_message_id=starter_message_id,
        ap_activity_id=ap_activity_id,
        ap_object_id=ap_object_id,
    )
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group.id,
        discord_channel_id=channel_id,
        discord_thread_id=thread_id,
        discord_starter_message_id=starter_message_id,
        role="source",
    )
    return thread_group


def _add_mirror_delivery(
    database: Database,
    *,
    thread_group_id: int,
    channel_id: int = 101,
    thread_id: int = 500,
    starter_message_id: int = 501,
) -> None:
    """Insert one mirror thread delivery row for a thread group."""
    database.discord_fanout_groups.add_thread_delivery(
        thread_group_id=thread_group_id,
        discord_channel_id=channel_id,
        discord_thread_id=thread_id,
        discord_starter_message_id=starter_message_id,
        role="mirror",
    )


def _fake_mirror_thread(
    *,
    thread_id: int = 500,
    sent_message_id: int = 600,
) -> SimpleNamespace:
    """Build a fake mirror Discord thread whose send() captures kwargs.

    The send mock stores call kwargs so tests can inspect the `reference`
    argument passed to thread.send().
    """
    fake_sent_message = SimpleNamespace(id=sent_message_id)
    fake_thread = SimpleNamespace(
        id=thread_id,
        send=AsyncMock(return_value=fake_sent_message),
    )
    return fake_thread


# ---------------------------------------------------------------------------
# Phase 4 scenario tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase4_root_message_mirrored_flat(tmp_path: Path) -> None:
    """Root message (no reference) is mirrored flat with no Discord reference.

    System state: two subscriptions (channels 100, 101), one registered user,
    thread group for thread 200 with source delivery, mirror delivery for thread 500.
    Source message has no reference field set.
    Action: handle_discord_message for message 400 in thread 200.
    Assert: mirror sent into thread 500 with reference=None (flat send),
    CommunityMessageGroup.parent_message_group_id is None.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _accepted_subscription(database, channel_id=101)
    _registered_user(database)
    thread_group = _create_thread_group_with_source_delivery(
        database, channel_id=100, thread_id=200, starter_message_id=300
    )
    _add_mirror_delivery(database, thread_group_id=thread_group.id)
    gateway = _publish_gateway()

    fake_thread = _fake_mirror_thread(thread_id=500, sent_message_id=600)
    fake_bot = SimpleNamespace(get_thread_by_id=AsyncMock(return_value=fake_thread))
    fanout = DiscordFanout(
        bot=fake_bot,
        mutation_tracker=fake_bot,
        database=database,
        policy_service=build_test_policy_service(database),
    )
    runtime = _community_runtime(database, gateway, discord_fanout=fanout)

    # No reference — root message.
    message = _fake_message(message_id=400, thread_id=200, channel_id=100)

    result = await runtime.handle_discord_message(message=message)

    message_group = database.discord_fanout_groups.get_message_group_by_source_message(400)
    assert result.status == "published"
    assert message_group is not None
    # Root message: no parent message group.
    assert message_group.parent_message_group_id is None
    # Mirror sent flat: reference kwarg must be None.
    fake_thread.send.assert_awaited_once()
    _, kwargs = fake_thread.send.call_args
    assert kwargs.get("reference") is None


@pytest.mark.asyncio
async def test_phase4_reply_to_starter_references_mirror_starter(tmp_path: Path) -> None:
    """Reply to the source thread starter uses the mirror thread's own starter as reference.

    System state: two subscriptions, thread group for thread 200 (starter 300),
    mirror delivery for thread 500 (starter 501).
    Source message references message 300 (the source starter).
    Action: handle_discord_message for message 400.
    Assert: mirror sent into thread 500 with reference.message_id=501,
    CommunityMessageGroup.parent_message_group_id is None.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _accepted_subscription(database, channel_id=101)
    _registered_user(database)
    thread_group = _create_thread_group_with_source_delivery(
        database, channel_id=100, thread_id=200, starter_message_id=300
    )
    _add_mirror_delivery(
        database, thread_group_id=thread_group.id,
        channel_id=101, thread_id=500, starter_message_id=501,
    )
    gateway = _publish_gateway()

    fake_thread = _fake_mirror_thread(thread_id=500, sent_message_id=600)
    fake_bot = SimpleNamespace(get_thread_by_id=AsyncMock(return_value=fake_thread))
    fanout = DiscordFanout(
        bot=fake_bot,
        mutation_tracker=fake_bot,
        database=database,
        policy_service=build_test_policy_service(database),
    )
    runtime = _community_runtime(database, gateway, discord_fanout=fanout)

    # Reference points to the source starter (message 300).
    message = _fake_message(
        message_id=400, thread_id=200, channel_id=100, reference_message_id=300
    )

    result = await runtime.handle_discord_message(message=message)

    message_group = database.discord_fanout_groups.get_message_group_by_source_message(400)
    assert result.status == "published"
    assert message_group is not None
    # Starter reply: parent_message_group_id is None (starter is not a message group).
    assert message_group.parent_message_group_id is None
    # Mirror must reference the mirror thread's own starter (501).
    fake_thread.send.assert_awaited_once()
    _, kwargs = fake_thread.send.call_args
    reference = kwargs.get("reference")
    assert reference is not None
    assert reference.message_id == 501


@pytest.mark.asyncio
async def test_phase4_reply_to_mirrored_message_references_mirror_delivery(tmp_path: Path) -> None:
    """Reply to a previously mirrored message uses the mirror delivery message as reference.

    System state: two subscriptions, thread group for thread 200 / mirror thread 500.
    Prior message group M exists with source delivery (thread 200, msg 400) and
    mirror delivery (thread 500, msg 600). New source message 401 references msg 400.
    Action: handle_discord_message for message 401.
    Assert: mirror sent into thread 500 with reference.message_id=600,
    new CommunityMessageGroup.parent_message_group_id = M.id.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _accepted_subscription(database, channel_id=101)
    _registered_user(database)
    thread_group = _create_thread_group_with_source_delivery(
        database, channel_id=100, thread_id=200, starter_message_id=300
    )
    _add_mirror_delivery(
        database, thread_group_id=thread_group.id,
        channel_id=101, thread_id=500, starter_message_id=501,
    )

    # Insert prior message group M (message 400 was already mirrored as 600 in thread 500).
    prior_group = database.discord_fanout_groups.create_message_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        thread_group_id=thread_group.id,
        source_channel_id=100,
        source_thread_id=200,
        source_message_id=400,
        ap_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/activities/create/comment/prior",
        ap_object_id=f"https://{BRIDGE_HOST_DOMAIN}/objects/comment/prior",
    )
    # Source delivery for prior message.
    database.discord_fanout_groups.add_message_delivery(
        message_group_id=prior_group.id,
        discord_channel_id=100,
        discord_thread_id=200,
        discord_message_id=400,
        role="source",
    )
    # Mirror delivery for prior message.
    database.discord_fanout_groups.add_message_delivery(
        message_group_id=prior_group.id,
        discord_channel_id=101,
        discord_thread_id=500,
        discord_message_id=600,
        role="mirror",
    )

    gateway = _publish_gateway()
    fake_thread = _fake_mirror_thread(thread_id=500, sent_message_id=700)
    fake_bot = SimpleNamespace(get_thread_by_id=AsyncMock(return_value=fake_thread))
    fanout = DiscordFanout(
        bot=fake_bot,
        mutation_tracker=fake_bot,
        database=database,
        policy_service=build_test_policy_service(database),
    )
    runtime = _community_runtime(database, gateway, discord_fanout=fanout)

    # New message 401 is a reply to the previously mirrored message 400.
    message = _fake_message(
        message_id=401, thread_id=200, channel_id=100, reference_message_id=400
    )

    result = await runtime.handle_discord_message(message=message)

    new_group = database.discord_fanout_groups.get_message_group_by_source_message(401)
    assert result.status == "published"
    assert new_group is not None
    # parent_message_group_id must point to M (the prior group).
    assert new_group.parent_message_group_id == prior_group.id
    # Mirror must reference the mirror delivery of msg 400, which is msg 600 in thread 500.
    fake_thread.send.assert_awaited_once()
    _, kwargs = fake_thread.send.call_args
    reference = kwargs.get("reference")
    assert reference is not None
    assert reference.message_id == 600


@pytest.mark.asyncio
async def test_phase4_reply_to_unknown_message_mirrored_flat(tmp_path: Path) -> None:
    """Reply to an unknown message (no delivery row) is mirrored flat without reference.

    System state: two subscriptions, thread group for thread 200 / mirror thread 500.
    Source message references message 999 — no CommunityMessageGroupDelivery row exists.
    Action: handle_discord_message for message 400.
    Assert: mirror sent into thread 500 with reference=None (flat fallback),
    CommunityMessageGroup.parent_message_group_id is None.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _accepted_subscription(database, channel_id=101)
    _registered_user(database)
    thread_group = _create_thread_group_with_source_delivery(
        database, channel_id=100, thread_id=200, starter_message_id=300
    )
    _add_mirror_delivery(database, thread_group_id=thread_group.id)
    gateway = _publish_gateway()

    fake_thread = _fake_mirror_thread(thread_id=500, sent_message_id=600)
    fake_bot = SimpleNamespace(get_thread_by_id=AsyncMock(return_value=fake_thread))
    fanout = DiscordFanout(
        bot=fake_bot,
        mutation_tracker=fake_bot,
        database=database,
        policy_service=build_test_policy_service(database),
    )
    runtime = _community_runtime(database, gateway, discord_fanout=fanout)

    # Reference to unknown message 999 — no delivery row exists.
    message = _fake_message(
        message_id=400, thread_id=200, channel_id=100, reference_message_id=999
    )

    result = await runtime.handle_discord_message(message=message)

    message_group = database.discord_fanout_groups.get_message_group_by_source_message(400)
    assert result.status == "published"
    assert message_group is not None
    # Unknown reference: parent_message_group_id stays None.
    assert message_group.parent_message_group_id is None
    # Flat fallback: reference must be None.
    fake_thread.send.assert_awaited_once()
    _, kwargs = fake_thread.send.call_args
    assert kwargs.get("reference") is None


@pytest.mark.asyncio
async def test_phase4_reply_db_consistency_mirror_delivery_recorded(tmp_path: Path) -> None:
    """Reply to a mirrored message: mirror delivery row recorded with correct message id.

    System state: two subscriptions, thread group for thread 200 / mirror thread 500.
    Prior message group M: source delivery (thread 200, msg 400), mirror delivery
    (thread 500, msg 600). New source message 401 references msg 400.
    Action: handle_discord_message for message 401.
    Assert: new CommunityMessageGroup has parent_message_group_id=M.id,
    new mirror delivery row in thread 500 has discord_message_id matching the sent
    mock return value (700), thread.send called with reference.message_id=600.
    """
    database = _database(tmp_path)
    _accepted_subscription(database, channel_id=100)
    _accepted_subscription(database, channel_id=101)
    _registered_user(database)
    thread_group = _create_thread_group_with_source_delivery(
        database, channel_id=100, thread_id=200, starter_message_id=300
    )
    _add_mirror_delivery(
        database, thread_group_id=thread_group.id,
        channel_id=101, thread_id=500, starter_message_id=501,
    )

    # Insert prior message group M.
    prior_group = database.discord_fanout_groups.create_message_group(
        community_actor_id=COMMUNITY_ACTOR_URL,
        thread_group_id=thread_group.id,
        source_channel_id=100,
        source_thread_id=200,
        source_message_id=400,
        ap_activity_id=f"https://{BRIDGE_HOST_DOMAIN}/activities/create/comment/prior",
        ap_object_id=f"https://{BRIDGE_HOST_DOMAIN}/objects/comment/prior",
    )
    database.discord_fanout_groups.add_message_delivery(
        message_group_id=prior_group.id,
        discord_channel_id=100,
        discord_thread_id=200,
        discord_message_id=400,
        role="source",
    )
    database.discord_fanout_groups.add_message_delivery(
        message_group_id=prior_group.id,
        discord_channel_id=101,
        discord_thread_id=500,
        discord_message_id=600,
        role="mirror",
    )

    gateway = _publish_gateway()
    # sent_message_id=700 — the newly created mirror of message 401 in thread 500.
    fake_thread = _fake_mirror_thread(thread_id=500, sent_message_id=700)
    fake_bot = SimpleNamespace(get_thread_by_id=AsyncMock(return_value=fake_thread))
    fanout = DiscordFanout(
        bot=fake_bot,
        mutation_tracker=fake_bot,
        database=database,
        policy_service=build_test_policy_service(database),
    )
    runtime = _community_runtime(database, gateway, discord_fanout=fanout)

    message = _fake_message(
        message_id=401, thread_id=200, channel_id=100, reference_message_id=400
    )

    result = await runtime.handle_discord_message(message=message)

    new_group = database.discord_fanout_groups.get_message_group_by_source_message(401)
    assert result.status == "published"
    assert new_group is not None
    assert new_group.parent_message_group_id == prior_group.id

    # Mirror delivery for message 401 in thread 500 must exist with message_id=700.
    new_deliveries = database.discord_fanout_groups.get_message_deliveries(new_group.id)
    mirror_deliveries = [d for d in new_deliveries if d.role == "mirror"]
    assert len(mirror_deliveries) == 1
    assert mirror_deliveries[0].discord_thread_id == 500
    assert mirror_deliveries[0].discord_message_id == 700

    # Thread send called with reference pointing to 600 (mirror of prior msg 400).
    fake_thread.send.assert_awaited_once()
    _, kwargs = fake_thread.send.call_args
    reference = kwargs.get("reference")
    assert reference is not None
    assert reference.message_id == 600
