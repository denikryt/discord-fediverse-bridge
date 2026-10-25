"""Recover an absent ActivityPub parent post through typed Discord transport.

Real file-backed SQLite owns subscriptions and canonical/delivery mappings; only
remote document retrieval and Discord HTTP are replaced by deterministic fakes.
"""
from __future__ import annotations

from support.fake_platform_adapter import FakeDiscordPort

from dataclasses import dataclass
from datetime import datetime, timezone
from types import ModuleType
import sys

import pytest

from src.community_sync.backfill import backfill_normalized_post_as_thread_group
from src.community_sync.inbound_mapping import needs_backfill
from src.community_sync.runtime import CommunityRuntime
from src.platform_types import CreatedThread, MessageRef, ThreadRef
from support.db import add_accepted_subscription, build_database
from tests_constants import LEMMY_EXAMPLE_DOMAIN

COMMUNITY = f'https://{LEMMY_EXAMPLE_DOMAIN}/c/hackers'
POST = f'https://{LEMMY_EXAMPLE_DOMAIN}/post/777'


@pytest.fixture(autouse=True)
def isolated_result_type(monkeypatch):
    """Replace SDK-dependent import of handler result, not domain repositories."""
    module = ModuleType('src.activitypub_handlers')

    @dataclass
    class HandlerResult:
        """Carry actual runtime outcome without importing Discord command tree."""
        status: str
        outcome: object
        detail: str

    module.HandlerResult = HandlerResult
    monkeypatch.setitem(sys.modules, 'src.activitypub_handlers', module)


def document(*, ap_id=POST):
    """Create a remote Lemmy Page with a stable ActivityPub object identity."""
    return {
        'type': 'Page', 'id': ap_id, 'name': 'A missing post',
        'attributedTo': f'https://{LEMMY_EXAMPLE_DOMAIN}/u/bob',
        'published': datetime.now(timezone.utc).isoformat(),
        'source': {'content': 'Some content'},
    }


class DiscordCreates(FakeDiscordPort):
    """Emulate confirmed per-forum Discord thread/starter pairs."""

    def __init__(self, *, bad_forum=None):
        """Remember actual sends and optionally return a foreign receipt."""
        self.calls = []
        self.bad_forum = bad_forum

    async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
        """Record the attempted POST and return an exact physical receipt."""
        self.calls.append((forum, content, title, attempt))
        if forum.external_channel_id == self.bad_forum:
            forum = type(forum)('discord-primary', '999', 'forum', '10')
        thread = ThreadRef(forum, str(int(forum.external_channel_id) + 1000))
        return CreatedThread(
            thread,
            MessageRef('discord-primary', thread.external_thread_id,
                       str(int(forum.external_channel_id) + 2000)), 'bot',
        )


def world(tmp_path, *, only_first=False):
    """Create a real subscribed Remote Community with independent forums."""
    db = build_database(tmp_path, 'ap-backfill.sqlite3')
    for forum_id in (100, 200):
        add_accepted_subscription(db, channel_id=forum_id, guild_id=10)
    group = None
    if only_first:
        group = db.discord_fanout_groups.create_thread_group(
            community_actor_id=COMMUNITY, source_channel_id=None,
            source_thread_id=None, source_starter_message_id=None,
            ap_activity_id='https://remote.example/previous-post-activity', ap_object_id=POST,
        )
        db.discord_fanout_groups.add_thread_delivery(
            thread_group_id=group.id, discord_channel_id=100,
            discord_thread_id=1100, discord_starter_message_id=2100, role='inbound',
        )
    return db, group, CommunityRuntime(database=db, content_publish_service=object())


async def restore(db, runtime, discord, *, doc=None):
    """Execute backfill using a fake remote fetcher but the real runtime/SQLite."""
    async def fetch(_url):
        """Return one configured AP document, verifying the requested identity."""
        assert _url == POST
        return doc if doc is not None else document()

    return await backfill_normalized_post_as_thread_group(
        post_ap_id=POST, community_actor_id=COMMUNITY,
        delivery_id='https://remote.example/create/comment', database=db,
        community_runtime=runtime, adapter=discord, fetch_ap_object=fetch,
    )


@pytest.mark.asyncio
async def test_missing_parent_is_created_once_with_distinct_thread_starter_ids(tmp_path):
    """Comment before post creates one canonical parent and two distinct copies."""
    db, _, runtime = world(tmp_path)
    discord = DiscordCreates()
    group = await restore(db, runtime, discord)
    assert group is not None
    assert {(r.discord_channel_id, r.discord_thread_id, r.discord_starter_message_id)
            for r in db.discord_fanout_groups.get_thread_deliveries(group.id)} == {
                (100, 1100, 2100), (200, 1200, 2200),
            }
    assert {call[3].key for call in discord.calls} == {
        f'fanout:v1:remote:post:{group.id}:discord-primary:'
        f'{db.platform_surfaces.resolve_claim("discord-primary", channel).id}'
        for channel in ('100', '200')
    }
    assert (await restore(db, runtime, discord)).id == group.id
    assert len(discord.calls) == 2


@pytest.mark.asyncio
async def test_partial_backfill_defers_unproven_missing_destination(tmp_path):
    """A partial old group cannot prove another target lacks a prior Discord thread."""
    db, existing, runtime = world(tmp_path, only_first=True)
    discord = DiscordCreates()
    # The missing receipt could be a committed thread whose HTTP response was lost.
    assert await restore(db, runtime, discord) is None
    rows = db.discord_fanout_groups.get_thread_deliveries(existing.id)
    assert {(r.discord_channel_id, r.discord_thread_id) for r in rows} == {(100, 1100)}
    assert discord.calls == []
    assert needs_backfill(thread_group=existing, community_actor_id=COMMUNITY, database=db) is True


@pytest.mark.asyncio
async def test_wrong_remote_object_cannot_create_arbitrary_parent(tmp_path):
    """A fetched Page claiming a different object ID must not create a post."""
    db, _, runtime = world(tmp_path)
    discord = DiscordCreates()
    assert await restore(db, runtime, discord, doc=document(ap_id='https://evil.example/post/1')) is None
    assert discord.calls == []
    assert db.discord_fanout_groups.get_thread_group_by_ap_object(POST) is None


@pytest.mark.asyncio
async def test_undo_follow_before_recovery_prevents_new_physical_copy(tmp_path):
    """A revoked subscriber cannot receive a newly recovered parent post."""
    db, existing, runtime = world(tmp_path, only_first=True)
    db.remote_subscriptions.delete_subscription(200)
    discord = DiscordCreates()
    result = await restore(db, runtime, discord)
    assert result.id == existing.id
    assert discord.calls == []


@pytest.mark.asyncio
async def test_wrong_physical_receipt_is_not_saved_or_reported_complete(tmp_path):
    """A foreign forum receipt must not satisfy a missing destination mapping."""
    db, existing, runtime = world(tmp_path, only_first=True)
    discord = DiscordCreates(bad_forum='200')
    assert await restore(db, runtime, discord) is None
    assert db.discord_fanout_groups.get_thread_deliveries(existing.id)[0].discord_channel_id == 100
    assert len(db.discord_fanout_groups.get_thread_deliveries(existing.id)) == 1


@pytest.mark.asyncio
async def test_failed_fetch_preserves_existing_canonical_delivery(tmp_path):
    """A remote outage leaves a partially mapped post untouched and deferred."""
    db, existing, runtime = world(tmp_path, only_first=True)
    async def fail(_url):
        """Simulate remote ActivityPub fetch timeout."""
        raise TimeoutError('remote unavailable')
    discord = DiscordCreates()
    result = await backfill_normalized_post_as_thread_group(
        post_ap_id=POST, community_actor_id=COMMUNITY,
        delivery_id='https://remote.example/create/comment', database=db,
        community_runtime=runtime, adapter=discord, fetch_ap_object=fail,
    )
    assert result is None and discord.calls == []
    assert db.discord_fanout_groups.get_thread_group_by_ap_object(POST).id == existing.id


def comment():
    """Build a comment arriving before the remote parent post was delivered."""
    from src.activitypub_models import ActivityPubEvent
    now = datetime.now(timezone.utc).isoformat()
    return ActivityPubEvent.model_validate({
        'event_type': 'comment.created',
        'delivery_id': 'https://remote.example/create/comment',
        'occurred_at': now, 'community_actor_id': COMMUNITY,
        'actor_id': f'https://{LEMMY_EXAMPLE_DOMAIN}/u/bob',
        'object': {
            'ap_id': f'https://{LEMMY_EXAMPLE_DOMAIN}/comment/42',
            'kind': 'comment', 'lemmy_id': 42, 'post_ap_id': POST,
            'post_lemmy_id': 777, 'parent_ap_id': POST,
            'body_markdown': 'This reply came first', 'url': POST,
            'published_at': now, 'author_name': 'bob',
        },
    })


class DiscordPostsAndComments(DiscordCreates):
    """Observe both recovered forum threads and their dependent comments."""

    def __init__(self):
        """Collect independently confirmed thread and comment writes."""
        super().__init__()
        self.message_calls = []

    async def send_message(self, destination, content, *, reply_to, presentation, attempt):
        """Return a separate receipt for every real destination thread."""
        from src.platform_types import CreatedMessage
        self.message_calls.append((destination, reply_to, attempt))
        return CreatedMessage(
            MessageRef('discord-primary', destination.external_thread_id,
                       str(int(destination.external_thread_id) + 4000)), 'bot',
        )


@pytest.mark.asyncio
async def test_normalized_comment_recovers_post_then_posts_to_both_mapped_threads(tmp_path, monkeypatch):
    """A comment-before-post creates exactly two parents then two child messages."""
    import src.bridge_lemmy_to_discord as legacy_ap
    async def fetch(ap_url):
        """Stand in for the remote GET at its network boundary."""
        assert ap_url == POST
        return document()
    monkeypatch.setattr(legacy_ap, '_fetch_ap_object', fetch)
    db, _, runtime = world(tmp_path)
    adapter = DiscordPostsAndComments()
    event = comment()
    result = await runtime.handle_normalized_inbound_comment_with_backfill(event, adapter=adapter)
    assert result.status == 'processed'
    assert len(adapter.calls) == len(adapter.message_calls) == 2
    assert {call[0].external_thread_id for call in adapter.message_calls} == {'1100', '1200'}
    assert all(call[1] is None for call in adapter.message_calls)
    group = db.discord_fanout_groups.get_message_group_by_ap_object(event.object.ap_id)
    assert group is not None
    assert len(db.discord_fanout_groups.get_message_deliveries(group.id)) == 2
    again = await runtime.handle_normalized_inbound_comment_with_backfill(event, adapter=adapter)
    assert again.status == 'skipped'
    assert len(adapter.calls) == len(adapter.message_calls) == 2


@pytest.mark.asyncio
async def test_initial_parent_partial_send_still_delivers_child_to_confirmed_forum(tmp_path, monkeypatch):
    """When first backfill has one bad receipt, its good thread gets the child."""
    import src.bridge_lemmy_to_discord as legacy_ap

    async def fetch(ap_url):
        """Fetch the matching original Page once at the remote network edge."""
        assert ap_url == POST
        return document()

    monkeypatch.setattr(legacy_ap, '_fetch_ap_object', fetch)
    db, _, runtime = world(tmp_path)
    adapter = DiscordPostsAndComments()
    adapter.bad_forum = '200'

    result = await runtime.handle_normalized_inbound_comment_with_backfill(
        comment(), adapter=adapter,
    )

    assert result.status == 'processed'
    assert {call[0].external_channel_id for call in adapter.calls} == {'100', '200'}
    assert [call[0].external_thread_id for call in adapter.message_calls] == ['1100']
    group = db.discord_fanout_groups.get_thread_group_by_ap_object(POST)
    assert group is not None
    assert {row.discord_channel_id for row in
            db.discord_fanout_groups.get_thread_deliveries(group.id)} == {100}
    msg_group = db.discord_fanout_groups.get_message_group_by_ap_object(comment().object.ap_id)
    assert msg_group is not None
    assert {row.discord_channel_id for row in
            db.discord_fanout_groups.get_message_deliveries(msg_group.id)} == {100}


@pytest.mark.asyncio
async def test_new_subscriber_does_not_block_confirmed_existing_comment_targets(tmp_path):
    """A later Follow cannot stall replies in two already-confirmed parent threads."""
    db, group, runtime = world(tmp_path, only_first=True)
    db.discord_fanout_groups.add_thread_delivery(
        thread_group_id=group.id, discord_channel_id=200,
        discord_thread_id=1200, discord_starter_message_id=2200, role='inbound',
    )
    add_accepted_subscription(db, channel_id=300, guild_id=10)
    adapter = DiscordPostsAndComments()
    event = comment()

    result = await runtime.handle_normalized_inbound_comment_with_backfill(event, adapter=adapter)

    assert result.status == 'processed'
    assert adapter.calls == []  # Never backfill the unproven third parent.
    assert {call[0].external_thread_id for call in adapter.message_calls} == {'1100', '1200'}
    msg_group = db.discord_fanout_groups.get_message_group_by_ap_object(event.object.ap_id)
    assert msg_group is not None
    assert {delivery.discord_channel_id for delivery in
            db.discord_fanout_groups.get_message_deliveries(msg_group.id)} == {100, 200}
    assert {delivery.discord_channel_id for delivery in
            db.discord_fanout_groups.get_thread_deliveries(group.id)} == {100, 200}
    assert (await runtime.handle_normalized_inbound_comment_with_backfill(
        event, adapter=adapter,
    )).status == 'skipped'
    assert len(adapter.message_calls) == 2


@pytest.mark.asyncio
async def test_missing_one_receipt_does_not_block_other_confirmed_comment_target(tmp_path):
    """An unknown create at forum 200 cannot suppress replies in forum 100."""
    db, group, runtime = world(tmp_path, only_first=True)
    adapter = DiscordPostsAndComments()
    event = comment()

    result = await runtime.handle_normalized_inbound_comment_with_backfill(event, adapter=adapter)

    assert result.status == 'processed'
    assert adapter.calls == []  # The lost parent receipt is not negative proof.
    assert [call[0].external_thread_id for call in adapter.message_calls] == ['1100']
    msg_group = db.discord_fanout_groups.get_message_group_by_ap_object(event.object.ap_id)
    assert msg_group is not None
    assert {delivery.discord_channel_id for delivery in
            db.discord_fanout_groups.get_message_deliveries(msg_group.id)} == {100}
    assert len(db.discord_fanout_groups.get_thread_deliveries(group.id)) == 1


@pytest.mark.asyncio
async def test_no_authorized_confirmed_parent_keeps_comment_deferred(tmp_path):
    """A revoked only receipt and an unknown destination permit no child create."""
    db, group, runtime = world(tmp_path, only_first=True)
    db.remote_subscriptions.delete_subscription(100)
    adapter = DiscordPostsAndComments()
    event = comment()

    result = await runtime.handle_normalized_inbound_comment_with_backfill(event, adapter=adapter)

    assert result.status == 'deferred'
    assert adapter.calls == adapter.message_calls == []
    assert db.discord_fanout_groups.get_message_group_by_ap_object(event.object.ap_id) is None
    assert len(db.discord_fanout_groups.get_thread_deliveries(group.id)) == 1


@pytest.mark.asyncio
async def test_incomplete_parent_only_delivers_child_to_verified_thread(tmp_path, monkeypatch):
    """An invalid or missing second parent never taints the verified first thread."""
    import src.bridge_lemmy_to_discord as legacy_ap
    async def fetch(ap_url):
        """Provide the real requested post, not a forged object."""
        return document()
    monkeypatch.setattr(legacy_ap, '_fetch_ap_object', fetch)
    db, existing, runtime = world(tmp_path, only_first=True)
    adapter = DiscordPostsAndComments()
    adapter.bad_forum = '200'
    result = await runtime.handle_normalized_inbound_comment_with_backfill(
        comment(), adapter=adapter,
    )
    assert result.status == 'processed'
    assert adapter.calls == []
    assert [call[0].external_thread_id for call in adapter.message_calls] == ['1100']
    msg_group = db.discord_fanout_groups.get_message_group_by_ap_object(comment().object.ap_id)
    assert msg_group is not None
    assert [row.discord_channel_id for row in db.discord_fanout_groups.get_message_deliveries(msg_group.id)] == [100]
    assert db.discord_fanout_groups.get_thread_group_by_ap_object(POST).id == existing.id


@pytest.mark.asyncio
async def test_foreign_community_canonical_post_cannot_be_rebound(tmp_path):
    """A canonical post owned by another community blocks refetch and all sends."""
    db, existing, runtime = world(tmp_path, only_first=True)
    # This simulates a persisted historical identity claim, not a new source event.
    with db.session() as session:
        from src.models import CommunityThreadGroup
        session.get(CommunityThreadGroup, existing.id).community_actor_id = 'https://other.example/c/foreign'
        session.commit()
    adapter = DiscordCreates()
    assert await restore(db, runtime, adapter) is None
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_unverified_new_subscriber_cannot_receive_backfilled_post(tmp_path):
    """Accepted-but-unverified guild metadata is not enough to send a thread."""
    db, existing, runtime = world(tmp_path, only_first=True)
    db.remote_subscriptions.delete_subscription(200)
    db.remote_subscriptions.create_subscription(
        discord_channel_id=300, discord_guild_id=None,
        lemmy_community_actor_id=COMMUNITY, lemmy_community_name='hackers',
        lemmy_community_id=42, status='accepted',
    )
    discord = DiscordCreates()
    assert await restore(db, runtime, discord) is None
    assert discord.calls == []
    assert len(db.discord_fanout_groups.get_thread_deliveries(existing.id)) == 1


@pytest.mark.asyncio
async def test_incomplete_group_does_not_refetch_ap_or_create_thread(tmp_path):
    """A real AP Page can prove authorship, not negative Discord delivery."""
    db, existing, runtime = world(tmp_path, only_first=True)
    adapter = DiscordCreates()
    requests = []

    async def fetch(_url):
        """Expose whether the unsafe recovery path tries to fetch the AP post."""
        requests.append(_url)
        return document()

    result = await backfill_normalized_post_as_thread_group(
        post_ap_id=POST, community_actor_id=COMMUNITY,
        delivery_id='https://remote.example/create/comment', database=db,
        community_runtime=runtime, adapter=adapter, fetch_ap_object=fetch,
    )
    assert result is None
    assert requests == []
    assert adapter.calls == []
    assert len(db.discord_fanout_groups.get_thread_deliveries(existing.id)) == 1

@pytest.mark.asyncio
async def test_deleted_canonical_parent_is_not_returned_by_backfill(tmp_path):
    """A retained physical starter is not permission to recover a deleted post."""
    db, group, runtime = world(tmp_path, only_first=True)
    db.discord_fanout_groups.add_thread_delivery(
        thread_group_id=group.id, discord_channel_id=200,
        discord_thread_id=1200, discord_starter_message_id=2200, role='inbound',
    )
    from src.models import CommunityThreadGroup
    with db.immediate_session() as session:
        session.get(CommunityThreadGroup, group.id).deleted_at = datetime.now(timezone.utc)

    adapter = DiscordPostsAndComments()
    assert await restore(db, runtime, adapter) is None
    assert adapter.calls == adapter.message_calls == []
    assert len(db.discord_fanout_groups.get_thread_deliveries(group.id)) == 2


@pytest.mark.asyncio
async def test_deleted_canonical_parent_rejects_late_child_before_admission(tmp_path):
    """Late AP comment cannot create a new canonical child under a tombstone."""
    db, group, runtime = world(tmp_path, only_first=True)
    from src.models import CommunityThreadGroup
    with db.immediate_session() as session:
        session.get(CommunityThreadGroup, group.id).deleted_at = datetime.now(timezone.utc)

    adapter = DiscordPostsAndComments()
    event = comment()
    response = await runtime.handle_normalized_inbound_comment_with_backfill(
        event, adapter=adapter,
    )
    assert response.status == 'skipped'
    assert adapter.calls == adapter.message_calls == []
    assert db.discord_fanout_groups.get_message_group_by_ap_object(event.object.ap_id) is None
    assert db.discord_fanout_groups.get_thread_group_by_ap_object(POST).deleted_at is not None
