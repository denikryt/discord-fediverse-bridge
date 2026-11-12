"""Remote ActivityPub Group fanout: one immutable object and claimed Discord target."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.activitypub_models import ActivityPubEvent
from src.community_sync.runtime import CommunityRuntime
from src.content_publish_service import ContentPublishService
from src.db import Database
from src.models import CommunityDiscordTargetClaim, CommunityThreadGroup
from support.runtime import build_test_policy_service

GROUP_A = "https://remote.example/category/1"
GROUP_B = "https://remote.example/category/2"
ROOT = "https://remote.example/post/42"
REPLY = "https://remote.example/post/43"
OWNER = "https://author.example/actors/alice"


def _event(group: str = GROUP_A, *, kind: str = "post", owner: str = OWNER, activity: str = "announce/1", parent: str | None = None) -> ActivityPubEvent:
    """Build a trusted normalized event with explicit root and original author."""
    return ActivityPubEvent.model_validate({
        "event_type": f"{kind}.created", "delivery_id": activity,
        "occurred_at": datetime.now(timezone.utc), "community_actor_id": group,
        "actor_id": owner,
        "object": {
            "ap_id": ROOT if kind == "post" else REPLY,
            "kind": kind, "lemmy_id": 0, "post_lemmy_id": 0,
            "post_ap_id": ROOT if kind == "comment" else None,
            "parent_ap_id": parent, "attributed_to_actor_id": owner,
            "title": "Topic", "body_markdown": "Content", "url": ROOT,
            "published_at": datetime.now(timezone.utc), "author_name": "alice",
        },
    })


def _setup(tmp_path: Path, channels: dict[int, tuple[int, int]], *, group: str = GROUP_A):
    """Create a real SQLite DB with accepted subscriptions and fake Discord boundary."""
    db = Database(f"sqlite:///{tmp_path / 'fanout.db'}")
    db.create_all()
    for channel in channels:
        db.remote_subscriptions.create_subscription(
            discord_channel_id=channel, lemmy_community_actor_id=group,
            lemmy_community_name="topic", lemmy_community_id=None,
            community_inbox_url=f"{group}/inbox", status="accepted",
        )
    forums = {}
    threads = {}
    for channel, (thread_id, starter_id) in channels.items():
        forums[channel] = SimpleNamespace(id=channel, create_thread=AsyncMock(return_value=(SimpleNamespace(id=thread_id), SimpleNamespace(id=starter_id))))
        threads[thread_id] = SimpleNamespace(id=thread_id, send=AsyncMock(return_value=SimpleNamespace(id=thread_id + 10000)))

    async def fetch_forum_channel(channel: int):
        return forums[channel]

    async def get_thread_by_id(thread_id: int):
        return threads[thread_id]

    async def ready():
        return None

    bot = SimpleNamespace(fetch_forum_channel=fetch_forum_channel, get_thread_by_id=get_thread_by_id, wait_until_bridge_ready=ready)
    policy = build_test_policy_service(db)
    publish = ContentPublishService(database=db, fedify_gateway=AsyncMock(), bridge_prefix="[bridge]", bridge_policy_service=policy)
    rt = CommunityRuntime(database=db, content_publish_service=publish, bot=bot)
    return db, rt, SimpleNamespace(bot=bot, database=db, bridge_policy_service=policy), forums, threads


@pytest.mark.asyncio
async def test_replay_completes_only_failed_channel_and_preserves_owner(tmp_path: Path) -> None:
    """A known, demonstrably unsent target may be completed without re-sending A."""
    db, rt, context, forums, _ = _setup(tmp_path, {100: (200, 300), 101: (201, 301)})
    try:
        # B is unavailable before any HTTP request; A still succeeds.
        first_fetch = context.bot.fetch_forum_channel
        fail = True

        async def fetch(channel: int):
            if channel == 101 and fail:
                raise LookupError("channel is not available")
            return await first_fetch(channel)

        context.bot.fetch_forum_channel = fetch
        result = await rt.handle_inbound_post(_event(), context)
        assert result.status == "processed"
        assert forums[100].create_thread.await_count == 1
        assert forums[101].create_thread.await_count == 0
        group = db.discord_fanout_groups.get_thread_group_by_ap_object(ROOT, community_actor_id=GROUP_A)
        assert group.source_owner_actor_id == OWNER
        assert len(db.discord_fanout_groups.get_thread_deliveries(group.id)) == 1
        fail = False
        await rt.handle_inbound_post(_event(activity="announce/2"), context)
        assert forums[100].create_thread.await_count == 1
        assert forums[101].create_thread.await_count == 1
        assert len(db.discord_fanout_groups.get_thread_deliveries(group.id)) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_ambiguous_discord_result_is_not_resent(tmp_path: Path) -> None:
    """An exception after the Discord call began is durable unknown, not failed."""
    db, rt, context, forums, _ = _setup(tmp_path, {100: (200, 300)})
    try:
        forums[100].create_thread.side_effect = TimeoutError("response lost")
        await rt.handle_inbound_post(_event(), context)
        await rt.handle_inbound_post(_event(activity="announce/2"), context)
        assert forums[100].create_thread.await_count == 1
        with db.session() as session:
            claims = session.scalars(select(CommunityDiscordTargetClaim)).all()
            assert len(claims) == 1
            assert claims[0].status == "unknown"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_foreign_owner_replay_rejected_without_discord(tmp_path: Path) -> None:
    """A later envelope cannot replace the canonical author or append a new target."""
    db, rt, context, forums, _ = _setup(tmp_path, {100: (200, 300)})
    try:
        await rt.handle_inbound_post(_event(), context)
        result = await rt.handle_inbound_post(
            _event(owner="https://evil.example/user", activity="announce/2"), context
        )
        assert result.status == "skipped"
        assert "owner" in result.detail
        assert forums[100].create_thread.await_count == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_concurrent_announcement_uses_one_target_claim(tmp_path: Path) -> None:
    """Two different Announce IDs cannot pass the pre-REST claim for one Group."""
    db, rt, context, forums, _ = _setup(tmp_path, {100: (200, 300)})
    started = asyncio.Event()
    finish = asyncio.Event()
    real_create = forums[100].create_thread

    async def blocked(**kwargs):
        started.set()
        await finish.wait()
        return await real_create(**kwargs)

    forums[100].create_thread = AsyncMock(side_effect=blocked)
    try:
        a = asyncio.create_task(rt.handle_inbound_post(_event(), context))
        await started.wait()
        b = asyncio.create_task(rt.handle_inbound_post(_event(activity="announce/2"), context))
        await asyncio.wait_for(b, 3)
        finish.set()
        await asyncio.wait_for(a, 3)
        assert forums[100].create_thread.await_count == 1
        with db.session() as session:
            assert len(session.scalars(select(CommunityThreadGroup)).all()) == 1
    finally:
        finish.set()
        db.close()


@pytest.mark.asyncio
async def test_reply_parent_scoped_to_current_group(tmp_path: Path) -> None:
    """A reply cannot use the root delivery and parent context of another Group."""
    db, rt, context, forums, threads = _setup(tmp_path, {100: (200, 300)})
    try:
        await rt.handle_inbound_post(_event(), context)
        db.remote_subscriptions.create_subscription(
            discord_channel_id=101, lemmy_community_actor_id=GROUP_B,
            lemmy_community_name="other", lemmy_community_id=None,
            status="accepted", community_inbox_url=f"{GROUP_B}/inbox",
        )
        result = await rt.handle_inbound_comment(_event(GROUP_B, kind="comment", parent=ROOT), context)
        assert result.status == "skipped"
        assert "Group" in result.detail
        assert threads[200].send.await_count == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_same_remote_object_can_be_announced_in_two_groups(tmp_path: Path) -> None:
    """Two accepted Groups with the same Note ID create independent canonical placements."""
    db, rt, context, forums, _ = _setup(tmp_path, {100: (200, 300), 101: (201, 301)})
    try:
        # Change the second subscription to a different verified Group.
        second = db.remote_subscriptions.get_subscription_by_channel(101)
        with db.session() as session:
            from src.models import ChannelCommunitySubscription
            row = session.get(ChannelCommunitySubscription, second.id)
            row.lemmy_community_actor_id = GROUP_B
            row.community_inbox_url = f"{GROUP_B}/inbox"
        a = await rt.handle_inbound_post(_event(GROUP_A), context)
        b = await rt.handle_inbound_post(_event(GROUP_B, activity="announce/b"), context)
        assert a.status == b.status == "processed"
        root_a = db.discord_fanout_groups.get_thread_group_by_ap_object(ROOT, community_actor_id=GROUP_A)
        root_b = db.discord_fanout_groups.get_thread_group_by_ap_object(ROOT, community_actor_id=GROUP_B)
        assert root_a.id != root_b.id
        assert len(db.discord_fanout_groups.get_thread_deliveries(root_a.id)) == 1
        assert len(db.discord_fanout_groups.get_thread_deliveries(root_b.id)) == 1
        assert forums[100].create_thread.await_count == 1
        assert forums[101].create_thread.await_count == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_reply_unknown_attempt_is_never_resent(tmp_path: Path) -> None:
    """A reply timeout cannot be interpreted as a definitely unsent Discord message."""
    db, rt, context, forums, threads = _setup(tmp_path, {100: (200, 300)})
    try:
        await rt.handle_inbound_post(_event(), context)
        threads[200].send.side_effect = TimeoutError("Discord accepted but response lost")
        first = await rt.handle_inbound_comment(_event(kind="comment", parent=ROOT), context)
        second = await rt.handle_inbound_comment(_event(kind="comment", parent=ROOT, activity="announce/2"), context)
        assert first.status == second.status == "skipped"
        assert threads[200].send.await_count == 1
        with db.session() as session:
            claims = session.scalars(select(CommunityDiscordTargetClaim).where(CommunityDiscordTargetClaim.kind == "comment")).all()
            assert len(claims) == 1
            assert claims[0].status == "unknown"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_reply_to_verified_root_is_delivered_once(tmp_path: Path) -> None:
    """A normal reply creates one immutable comment and only one Discord message."""
    db, rt, context, forums, threads = _setup(tmp_path, {100: (200, 300)})
    try:
        await rt.handle_inbound_post(_event(), context)
        first = await rt.handle_inbound_comment(_event(kind="comment", parent=ROOT), context)
        second = await rt.handle_inbound_comment(_event(kind="comment", parent=ROOT, activity="announce/2"), context)
        assert first.status == "processed"
        assert second.status == "skipped"
        assert threads[200].send.await_count == 1
        msg = db.discord_fanout_groups.get_message_group_by_ap_object(REPLY, community_actor_id=GROUP_A)
        assert msg.source_owner_actor_id == OWNER
        assert len(db.discord_fanout_groups.get_message_deliveries(msg.id)) == 1
    finally:
        db.close()
