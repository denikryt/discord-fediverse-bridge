"""Local Community AP Create reaches real SQLite receipts through the platform boundary."""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
import sys

import pytest

from src.platform_types import ChannelRef, CreatedMessage, CreatedThread, MessageRef, ThreadRef
from src.local_communities.runtime import LocalCommunityRuntime
from test_local_receipt_persistence import world
from support.fake_platform_adapter import FakeDiscordPort
from test_inbound_duplicate_admission import _post, _accepted


class Delivery(FakeDiscordPort):
    """Record genuine per-target physical writes and return independent confirmed IDs."""

    def __init__(self):
        """Start with no physical messages and no synthetic SDK client."""
        self.writes = []
        self.blocked_forum = None
        self.bad_receipt = False
        self.after_post = None
        self.message_counts = {}

    async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
        """Return separate forum thread/starter refs unless an injected fault occurs."""
        self.writes.append(('post', forum.external_channel_id, attempt.key))
        if forum.external_channel_id == self.blocked_forum:
            raise RuntimeError('transport unavailable')
        tid = {'300': '400', '200': '500', '201': '600'}.get(forum.external_channel_id, '900')
        if self.after_post is not None:
            self.after_post()
        if self.bad_receipt:
            return CreatedThread(ThreadRef(ChannelRef('discord-primary', '999', 'forum', '10'), tid),
                                 MessageRef('discord-primary', tid, '401'), 'bot')
        return CreatedThread(ThreadRef(forum, tid), MessageRef('discord-primary', tid,
                             str(int(tid) + 1)), 'bot')

    async def send_message(self, destination, content, *, reply_to, presentation, attempt):
        """Observe nested reply references and return host/sibling physical IDs."""
        self.writes.append(('comment', destination.external_thread_id, reply_to.external_message_id if reply_to else None))
        count = self.message_counts.get(destination.external_thread_id, 0)
        self.message_counts[destination.external_thread_id] = count + 1
        base = 700 if destination.external_thread_id == '400' else 800
        return CreatedMessage(MessageRef('discord-primary', destination.external_thread_id,
                              str(base + count)), 'bot')


def assert_unresolved_without_host(db, object_id: str, *, kind: str) -> None:
    """An unverified POST is durable uncertainty, never a valid host receipt."""
    from sqlalchemy import select
    from src.models import PlatformDeliveryIntent
    canonical = (db.local_community_content.get_local_community_thread_by_ap_object_id(object_id)
                 if kind == 'post' else
                 db.local_community_content.get_local_community_message_by_ap_object_id(object_id))
    assert canonical is not None and canonical.current_content_json is not None
    with db.session() as session:
        rows = list(session.scalars(select(PlatformDeliveryIntent).where(
            PlatformDeliveryIntent.domain == 'local',
            PlatformDeliveryIntent.object_kind == kind,
            PlatformDeliveryIntent.canonical_row_id == canonical.id,
        )))
        assert len(rows) == 1 and rows[0].state == 'unresolved'
    if kind == 'post':
        assert db.local_community_surfaces.get_host_local_community_thread_surface(canonical.id) is None
    else:
        assert db.local_community_surfaces.get_host_local_community_message_surface(canonical.id) is None


@pytest.fixture(autouse=True)
def handler_contract(monkeypatch):
    """Stub only the SDK-dependent top-level HTTP handler result module."""
    module = ModuleType('src.activitypub_handlers')

    @dataclass
    class HandlerResult:
        """Preserve the HTTP admission result shape."""
        status: str
        outcome: object
        detail: str

    module.HandlerResult = HandlerResult
    monkeypatch.setitem(sys.modules, 'src.activitypub_handlers', module)


def setup(tmp_path, *, allowed=True):
    """Prepare a real local community, accepted remote follower and live adapter binding."""
    db, community_id = world(tmp_path)
    _accepted(db, community_id)
    relays = []
    runtime = LocalCommunityRuntime(
        database=db, fedify_gateway=object(), content_publish_service=object(),
        bridge_prefix='[bridge]',
        bridge_policy_service=SimpleNamespace(is_discord_guild_allowed=lambda guild: allowed and guild == 10),
    )
    async def relay(**kwargs):
        """Record outbound federation without contacting external servers."""
        relays.append(kwargs['object_kind'])
    runtime.federation_fanout.relay_create = relay
    adapter = Delivery()
    runtime.bind_discord_adapter(adapter)
    return db, runtime, adapter, relays


def comment(*, parent='https://remote.test/post', obj='https://remote.test/comment'):
    """Build a source-evidenced remote comment belonging to the canonical post."""
    activity = 'https://remote.test/create/comment'
    author = 'https://remote.test/u/bob'
    return SimpleNamespace(
        delivery_id=activity, source_activity_id=activity, source_announce_id=None,
        source_activity_json={'id': activity, 'type': 'Create', 'actor': author,
                              'object': {'id': obj, 'content': 'Reply'}},
        community_actor_id='https://bridge.test/actors/communities/hackers', actor_id=author,
        object=SimpleNamespace(ap_id=obj, post_ap_id='https://remote.test/post',
                               parent_ap_id=parent, author_name='bob', body_markdown='Reply'),
    )


@pytest.mark.asyncio
async def test_verified_ap_post_creates_host_receipt_and_replay_never_posts_again(tmp_path):
    """An accepted Create stores one physical host receipt; replay has no second POST."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        result = await runtime.handle_inbound_post(_post(), SimpleNamespace())
        row = db.local_community_content.get_local_community_thread_by_ap_object_id('https://remote.test/post')
        host = db.local_community_surfaces.get_host_local_community_thread_surface(row.id)
        assert result.status == 'processed'
        assert (host.discord_thread_id, host.discord_starter_message_id) == (400, 401)
        assert len(adapter.writes) == 1 and relays == ['post']
        again = await runtime.handle_inbound_post(_post(), SimpleNamespace())
        assert again.status == 'skipped'
        assert len(adapter.writes) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_comment_and_nested_reply_use_exact_host_thread_and_parent(tmp_path):
    """After a verified post, remote reply maps exact root/starter and canonical ID."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        await runtime.handle_inbound_post(_post(), SimpleNamespace())
        result = await runtime.handle_inbound_comment(comment(), SimpleNamespace())
        row = db.local_community_content.get_local_community_message_by_ap_object_id('https://remote.test/comment')
        assert result.status == 'processed'
        assert db.local_community_surfaces.get_host_local_community_message_surface(row.id).discord_message_id == 700
        assert adapter.writes[-1] == ('comment', '400', '401')
        again = await runtime.handle_inbound_comment(comment(), SimpleNamespace())
        assert again.status == 'skipped'
        assert len([x for x in adapter.writes if x[0] == 'comment']) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_untrusted_host_receipt_never_persists_or_relays(tmp_path):
    """A transport receipt with the wrong forum cannot claim a canonical Discord host."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        adapter.bad_receipt = True
        result = await runtime.handle_inbound_post(_post(), SimpleNamespace())
        assert result.status == 'processed'
        assert_unresolved_without_host(db, 'https://remote.test/post', kind='post')
        assert relays == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_disallowed_host_never_sends(tmp_path):
    """A changed guild allowlist prevents physical creation at the authority boundary."""
    db, runtime, adapter, relays = setup(tmp_path, allowed=False)
    try:
        result = await runtime.handle_inbound_post(_post(), SimpleNamespace())
        assert result.status == 'skipped'
        assert adapter.writes == [] and relays == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_two_subscriber_surfaces_isolate_failed_target_and_resume_only_missing(tmp_path):
    """Host and one healthy subscriber persist independently when another forum fails."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        db.local_subscribers.create_local_subscriber(
            local_community_id=db.local_communities.get_local_community_by_slug('hackers').id,
            discord_guild_id=10, discord_channel_id=200,
            initiated_by_discord_user_id='123',
        )
        db.local_subscribers.create_local_subscriber(
            local_community_id=db.local_communities.get_local_community_by_slug('hackers').id,
            discord_guild_id=10, discord_channel_id=201,
            initiated_by_discord_user_id='123',
        )
        adapter.blocked_forum = '201'
        assert (await runtime.handle_inbound_post(_post(), SimpleNamespace())).status == 'processed'
        assert {forum for operation, forum, _ in adapter.writes if operation == 'post'} == {'300','200','201'}
        assert relays == ['post']
        adapter.blocked_forum = None
        second = await runtime.handle_inbound_post(_post(), SimpleNamespace())
        assert second.status == 'skipped'
        # An exception after a committed dispatch does not prove Discord did
        # not create the thread: no automatic retry is safe on AP replay.
        assert [forum for operation, forum, _ in adapter.writes if operation == 'post'].count('300') == 1
        assert [forum for operation, forum, _ in adapter.writes if operation == 'post'].count('200') == 1
        assert [forum for operation, forum, _ in adapter.writes if operation == 'post'].count('201') == 1
        from sqlalchemy import select
        from src.models import PlatformDeliveryIntent
        with db.session() as session:
            missing = session.scalar(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.domain == 'local',
                PlatformDeliveryIntent.state == 'unresolved',
            ))
            assert missing is not None and missing.channel_surface_id is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_foreign_nested_parent_and_forged_replay_never_send_or_relay(tmp_path):
    """Known foreign parent or changed duplicate origin has no Discord/AP side effects."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        await runtime.handle_inbound_post(_post(), SimpleNamespace())
        # A legitimate reply in a different canonical thread cannot be a reply parent.
        other = db.local_community_content.create_local_community_thread(
            local_community_id=db.local_communities.get_local_community_by_slug('hackers').id,
            discord_thread_id=900, discord_starter_message_id=901,
            ap_activity_id='https://remote.test/create/other',
            ap_object_id='https://remote.test/other',
            direction='ap_to_discord', origin_kind='remote_follower',
        )
        db.local_community_content.create_local_community_message(
            local_community_thread_id=other.id, discord_message_id=902,
            ap_activity_id='https://remote.test/create/foreign',
            ap_object_id='https://remote.test/foreign', parent_ap_object_id='https://remote.test/other',
            parent_discord_message_id=901, direction='ap_to_discord',
        )
        before = len(adapter.writes)
        result = await runtime.handle_inbound_comment(
            comment(parent='https://remote.test/foreign'), SimpleNamespace(),
        )
        assert result.status == 'skipped' and len(adapter.writes) == before
        assert relays == ['post']
        changed = _post(body='forged content')
        conflict = await runtime.handle_inbound_post(changed, SimpleNamespace())
        assert conflict.status == 'skipped' and len(adapter.writes) == before
        assert relays == ['post']
    finally:
        db.close()


@pytest.mark.asyncio
async def test_revoked_remote_subscriber_cannot_recover_missing_forum_copy(tmp_path):
    """After Undo(Follow), replay is rejected even if a recipient was previously unavailable."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        await runtime.handle_inbound_post(_post(), SimpleNamespace())
        community = db.local_communities.get_local_community_by_slug('hackers')
        subscriber = db.remote_subscribers.get_remote_subscriber(
            local_community_id=community.id, remote_actor_id='https://remote.test/u/bob',
        )
        # Use the same persisted authorization repository as the Follow handler.
        subscriber.status = 'revoked'
        with db.session() as session:
            from src.models import RemoteSubscriber
            saved = session.get(RemoteSubscriber, subscriber.id)
            saved.status = 'revoked'
        before = list(adapter.writes)
        result = await runtime.handle_inbound_post(_post(), SimpleNamespace())
        assert result.status == 'skipped' and adapter.writes == before and relays == ['post']
    finally:
        db.close()


@pytest.mark.asyncio
async def test_undo_follow_during_discord_create_stores_receipt_but_stops_relay(tmp_path):
    """An in-flight physical POST cannot be undone, but later AP relay must stop."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        def revoke():
            """Model a concurrent committed Undo(Follow) before Discord responds."""
            from src.models import RemoteSubscriber
            subscriber = db.remote_subscribers.get_remote_subscriber(
                local_community_id=db.local_communities.get_local_community_by_slug('hackers').id,
                remote_actor_id='https://remote.test/u/bob',
            )
            with db.session() as session:
                session.get(RemoteSubscriber, subscriber.id).status = 'revoked'
        adapter.after_post = revoke
        result = await runtime.handle_inbound_post(_post(), SimpleNamespace())
        assert result.status == 'skipped'
        assert db.local_community_content.get_local_community_thread_by_ap_object_id('https://remote.test/post') is not None
        assert relays == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_host_rebinding_during_create_rejects_old_receipt(tmp_path):
    """A changed channel binding must not store old physical ID under a new host."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        def rebind():
            """Change the authoritative channel surface while HTTP is in flight."""
            from sqlalchemy import select
            from src.models import ChannelSurface
            with db.session() as session:
                surface = session.scalar(select(ChannelSurface).where(
                    ChannelSurface.external_channel_id == '300'))
                surface.external_channel_id = '301'
        adapter.after_post = rebind
        result = await runtime.handle_inbound_post(_post(), SimpleNamespace())
        assert result.status == 'processed'
        row = db.local_community_content.get_local_community_thread_by_ap_object_id('https://remote.test/post')
        assert row is not None
        assert db.local_community_surfaces.get_host_local_community_thread_surface(row.id) is None
        from sqlalchemy import select
        from src.models import PlatformDeliveryIntent
        with db.session() as session:
            intent = session.scalar(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.canonical_row_id == row.id,
                PlatformDeliveryIntent.object_kind == 'post',
                PlatformDeliveryIntent.domain == 'local'))
            assert intent is not None and intent.state == 'cancelled_after_send'
            assert '400' in intent.orphan_receipt_json and '401' in intent.orphan_receipt_json
        assert relays == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_nested_comment_uses_each_destinations_actual_parent_receipt(tmp_path):
    """The host and subscriber must not borrow each other's nested reply IDs."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        community = db.local_communities.get_local_community_by_slug('hackers')
        db.local_subscribers.create_local_subscriber(
            local_community_id=community.id, discord_guild_id=10,
            discord_channel_id=200, initiated_by_discord_user_id='123',
        )
        await runtime.handle_inbound_post(_post(), SimpleNamespace())
        await runtime.handle_inbound_comment(comment(), SimpleNamespace())
        child = comment(parent='https://remote.test/comment', obj='https://remote.test/comment/child')
        child.delivery_id = 'https://remote.test/create/comment/child'
        child.source_activity_id = child.delivery_id
        child.source_activity_json = {'id': child.delivery_id, 'type': 'Create',
                                      'actor': child.actor_id, 'object': {'id': child.object.ap_id, 'content': 'Reply'}}
        result = await runtime.handle_inbound_comment(child, SimpleNamespace())
        assert result.status == 'processed'
        # Host reply 700 and subscriber reply 800 were independently persisted.
        assert ('comment', '400', '700') in adapter.writes
        assert ('comment', '500', '800') in adapter.writes
    finally:
        db.close()

@pytest.mark.asyncio
async def test_foreign_nested_parent_rejected_before_durable_comment_admission(tmp_path):
    """Cross-thread AP parent cannot leave a durable inbound object or physical copy."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        await runtime.handle_inbound_post(_post(), SimpleNamespace())
        community = db.local_communities.get_local_community_by_slug('hackers')
        other = db.local_community_content.create_local_community_thread(
            local_community_id=community.id, discord_thread_id=910,
            discord_starter_message_id=911,
            ap_activity_id='https://remote.test/create/another-post',
            ap_object_id='https://remote.test/another-post',
            direction='ap_to_discord', origin_kind='remote_follower',
        )
        db.local_community_content.create_local_community_message(
            local_community_thread_id=other.id, discord_message_id=912,
            ap_activity_id='https://remote.test/create/another-reply',
            ap_object_id='https://remote.test/another-reply',
            parent_ap_object_id=other.ap_object_id, parent_discord_message_id=911,
            direction='ap_to_discord',
        )
        before = list(adapter.writes)
        result = await runtime.handle_inbound_comment(
            comment(parent='https://remote.test/another-reply'), SimpleNamespace(),
        )
        assert result.status == 'skipped' and adapter.writes == before
        assert db.personal_feed_conversations.get_inbound_object('https://remote.test/comment') is None
        assert db.local_community_content.get_local_community_message_by_ap_object_id('https://remote.test/comment') is None
        assert relays == ['post']
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unmapped_nested_parent_waits_for_verified_host_mapping(tmp_path):
    """A missing nested parent cannot be flattened and can be retried later."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        await runtime.handle_inbound_post(_post(), SimpleNamespace())
        pending = comment(parent='https://remote.test/missing', obj='https://remote.test/pending')
        pending.delivery_id = 'https://remote.test/create/pending'
        pending.source_activity_id = pending.delivery_id
        pending.source_activity_json = {'id': pending.delivery_id, 'type': 'Create',
                                        'actor': pending.actor_id, 'object': {'id': pending.object.ap_id}}
        before = list(adapter.writes)
        result = await runtime.handle_inbound_comment(pending, SimpleNamespace())
        assert result.status == 'skipped' and adapter.writes == before
        assert db.personal_feed_conversations.get_inbound_object('https://remote.test/pending') is None
        parent = comment(obj='https://remote.test/missing')
        parent.delivery_id = 'https://remote.test/create/missing'
        parent.source_activity_id = parent.delivery_id
        parent.source_activity_json = {'id': parent.delivery_id, 'type': 'Create',
                                       'actor': parent.actor_id, 'object': {'id': parent.object.ap_id}}
        assert (await runtime.handle_inbound_comment(parent, SimpleNamespace())).status == 'processed'
        assert (await runtime.handle_inbound_comment(pending, SimpleNamespace())).status == 'processed'
        assert ('comment', '400', '700') in adapter.writes
    finally:
        db.close()


@pytest.mark.asyncio
async def test_stale_host_surface_rejected_before_durable_comment_admission(tmp_path):
    """Host rebinding cannot strand an accepted comment without a physical copy."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        await runtime.handle_inbound_post(_post(), SimpleNamespace())
        from sqlalchemy import select
        from src.models import ChannelSurface
        with db.session() as session:
            host = session.scalar(select(ChannelSurface).where(ChannelSurface.external_channel_id == '300'))
            host.external_channel_id = '301'
        before = list(adapter.writes)
        result = await runtime.handle_inbound_comment(comment(), SimpleNamespace())
        assert result.status == 'skipped' and adapter.writes == before
        assert db.personal_feed_conversations.get_inbound_object('https://remote.test/comment') is None
        assert db.local_community_content.get_local_community_message_by_ap_object_id('https://remote.test/comment') is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_receipt_delivery_mode_must_match_requested_bot_presentation(tmp_path):
    """A misreported webhook receipt cannot claim a bot-mode physical surface."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        original_post = adapter.create_forum_thread
        async def forged_post(forum, content, *, title, presentation, attempt):
            """Pretend the remote platform created a webhook instead of a bot post."""
            receipt = await original_post(forum, content, title=title,
                                          presentation=presentation, attempt=attempt)
            return CreatedThread(receipt.thread, receipt.starter, 'webhook')
        adapter.create_forum_thread = forged_post
        from src.platform_types import PlatformOperationError
        assert (await runtime.handle_inbound_post(_post(), SimpleNamespace())).status == 'processed'
        assert_unresolved_without_host(db, 'https://remote.test/post', kind='post')
        assert relays == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_comment_receipt_delivery_mode_must_match_requested_bot_presentation(tmp_path):
    """A wrong physical send mode cannot be durably mislabeled as bot delivery."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        await runtime.handle_inbound_post(_post(), SimpleNamespace())
        original_send = adapter.send_message
        async def forged_send(destination, content, *, reply_to, presentation, attempt):
            """Return a structurally valid message from the wrong delivery mode."""
            receipt = await original_send(destination, content, reply_to=reply_to,
                                          presentation=presentation, attempt=attempt)
            return CreatedMessage(receipt.message, 'webhook')
        adapter.send_message = forged_send
        from src.platform_types import PlatformOperationError
        assert (await runtime.handle_inbound_comment(comment(), SimpleNamespace())).status == 'processed'
        assert_unresolved_without_host(db, 'https://remote.test/comment', kind='comment')
        assert relays == ['post']
    finally:
        db.close()

@pytest.mark.asyncio
async def test_foreign_community_post_cannot_own_local_reply(tmp_path):
    """A mapped post in another local community is not this community's parent."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        from sqlalchemy import select
        from src.models import ChannelSurface, LocalCommunity, PlatformConnector
        with db.immediate_session() as session:
            connector = session.scalar(select(PlatformConnector).where(
                PlatformConnector.connector_key == 'discord-primary'))
            foreign = LocalCommunity(
                slug='other', display_name='Other', actor_url='https://bridge.test/actors/communities/other',
                inbox_url='https://bridge.test/actors/communities/other/inbox',
                outbox_url='https://bridge.test/actors/communities/other/outbox',
                followers_url='https://bridge.test/actors/communities/other/followers',
                public_key_pem='public', private_key_pem='private',
            )
            session.add(foreign)
            session.flush()
            foreign_id = foreign.id
            session.add(ChannelSurface(connector_id=connector.id, external_channel_id='302',
                                       external_guild_id='10', kind='forum',
                                       local_community_id=foreign_id))
        db.local_community_content.create_local_community_thread(
            local_community_id=foreign_id, discord_thread_id=950,
            discord_starter_message_id=951,
            ap_activity_id='https://remote.test/create/foreign-post',
            ap_object_id='https://remote.test/foreign-post',
            direction='ap_to_discord', origin_kind='remote_follower',
        )
        reply = comment(parent='https://remote.test/foreign-post',
                        obj='https://remote.test/wrong-community')
        reply.object.post_ap_id = 'https://remote.test/foreign-post'
        reply.delivery_id = 'https://remote.test/create/wrong-community'
        reply.source_activity_id = reply.delivery_id
        reply.source_activity_json = {'id': reply.delivery_id, 'type': 'Create',
                                      'actor': reply.actor_id, 'object': {'id': reply.object.ap_id}}
        outcome = await runtime.handle_inbound_comment(reply, SimpleNamespace())
        assert outcome.status == 'skipped' and adapter.writes == [] and relays == []
        assert db.personal_feed_conversations.get_inbound_object(reply.object.ap_id) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unknown_forum_metadata_never_becomes_a_host_receipt(tmp_path):
    """A reply cannot adopt a purported forum thread with unverified guild/kind."""
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        original_post = adapter.create_forum_thread
        async def unverified_post(forum, content, *, title, presentation, attempt):
            """Drop verified parent kind/guild while preserving physical forum ID."""
            receipt = await original_post(forum, content, title=title,
                                          presentation=presentation, attempt=attempt)
            return CreatedThread(ThreadRef(ChannelRef('discord-primary', forum.external_channel_id,
                                                     'unknown', None), receipt.thread.external_thread_id),
                                 receipt.starter, 'bot')
        adapter.create_forum_thread = unverified_post
        from src.platform_types import PlatformOperationError
        assert (await runtime.handle_inbound_post(_post(), SimpleNamespace())).status == 'processed'
        assert_unresolved_without_host(db, 'https://remote.test/post', kind='post')
        assert relays == []
    finally:
        db.close()

@pytest.mark.asyncio
async def test_known_nested_parent_blocks_until_same_thread_receipt_is_verified(tmp_path):
    """A known AP comment cannot flatten or send before its exact host parent."""
    from sqlalchemy import select
    from src.models import PlatformDeliveryIntent
    from src.content_sync.platform_fanout import PlatformFanoutCoordinator
    from src.platform_types import ContentDocument, DeliveryPresentation, CreateAttempt
    from src.local_communities.rendering import format_inbound_comment_body

    db, runtime, adapter, relays = setup(tmp_path)
    try:
        await runtime.handle_inbound_post(_post(), SimpleNamespace())
        thread = db.local_community_content.get_local_community_thread_by_ap_object_id(
            'https://remote.test/post')
        parent_event = comment(parent='https://remote.test/post',
                               obj='https://remote.test/comment/parent')
        parent_event.delivery_id = 'https://remote.test/create/comment/parent'
        parent_event.source_activity_id = parent_event.delivery_id
        parent_event.source_activity_json = {
            'id': parent_event.delivery_id, 'actor': parent_event.actor_id,
            'type': 'Create', 'object': {'id': parent_event.object.ap_id},
        }
        # This real canonical row exists, but no Discord message has been
        # confirmed; the reply must retain its exact dependency in SQLite.
        parent = db.local_community_content.admit_inbound_comment(
            local_community_thread_id=thread.id,
            ap_activity_id=parent_event.delivery_id,
            ap_object_id=parent_event.object.ap_id,
            parent_ap_object_id=thread.ap_object_id,
            current_content_json=runtime.inbound_create_handler._inbound_snapshot(
                parent_event, kind='comment'),
        )
        child_event = comment(parent=parent.ap_object_id,
                              obj='https://remote.test/comment/child')
        child_event.delivery_id = 'https://remote.test/create/comment/child'
        child_event.source_activity_id = child_event.delivery_id
        child_event.source_activity_json = {
            'id': child_event.delivery_id, 'actor': child_event.actor_id,
            'type': 'Create', 'object': {'id': child_event.object.ap_id},
        }
        baseline = len(adapter.writes)
        outcome = await runtime.handle_inbound_comment(child_event, SimpleNamespace())
        assert outcome.status == 'processed' and 'blocked' in outcome.detail
        child = db.local_community_content.get_local_community_message_by_ap_object_id(
            child_event.object.ap_id)
        with db.session() as session:
            intent = session.scalar(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.domain == 'local',
                PlatformDeliveryIntent.object_kind == 'comment',
                PlatformDeliveryIntent.canonical_row_id == child.id))
            assert intent is not None and intent.state == 'blocked'
            assert intent.blocked_parent_canonical_id == parent.id
            assert intent.request_json is None and intent.dispatch_token is None
            blocked_id, intent_key = intent.id, intent.attempt_key
        assert len(adapter.writes) == baseline
        forum = ChannelRef('discord-primary', '300', 'forum', '10')
        destination = ThreadRef(forum, '400')
        coordinator = PlatformFanoutCoordinator.from_adapter(db, adapter)
        # A forged parent ID cannot materialize or dispatch the blocked child.
        attempt = CreateAttempt(intent_key, 'discord-primary', 'message', destination)
        with pytest.raises(ValueError):
            db.platform_delivery_intents.materialize(
                blocked_id, attempt=attempt,
                content=ContentDocument(format_inbound_comment_body(child_event)),
                presentation=DeliveryPresentation(),
                reply_to=MessageRef('discord-primary', '400', '999999'),
                title=None)
        assert await coordinator.stage_and_deliver(
            domain='local', kind='comment', canonical_id=parent.id,
            forum=forum, destination=destination, operation='message',
            content=ContentDocument(format_inbound_comment_body(parent_event)),
            presentation=DeliveryPresentation(),
            reply_to=MessageRef('discord-primary', '400', '401'),
        )
        # A verified parent receipt wakes exactly the persisted dependent
        # intent. The child is materialized from canonical SQLite content and
        # sent with its *actual* parent message reference, not a flat reply.
        with db.session() as session:
            final_intent = session.get(PlatformDeliveryIntent, blocked_id)
            assert final_intent.state == 'delivered'
            assert final_intent.blocked_parent_canonical_id is None
            assert final_intent.request_json is not None
        host = db.local_community_surfaces.get_host_local_community_message_surface(child.id)
        assert host is not None and host.discord_message_id == 701
        assert adapter.writes[-1] == ('comment', '400', '700')
        assert not await coordinator.deliver(blocked_id)
    finally:
        db.close()

@pytest.mark.asyncio
async def test_canonical_tombstones_block_replayed_local_ap_create(tmp_path):
    """A once-valid signed AP Create replay cannot revive a deleted post/comment."""
    from datetime import datetime, timezone
    from src.models import LocalCommunityThread, LocalCommunityMessage
    db, runtime, adapter, relays = setup(tmp_path)
    try:
        post = _post()
        await runtime.handle_inbound_post(post, SimpleNamespace())
        reply = comment()
        await runtime.handle_inbound_comment(reply, SimpleNamespace())
        prior = tuple(adapter.writes)
        source_post = db.local_community_content.get_local_community_thread_by_ap_object_id(post.object.ap_id)
        source_comment = db.local_community_content.get_local_community_message_by_ap_object_id(reply.object.ap_id)
        with db.immediate_session() as session:
            session.get(LocalCommunityMessage, source_comment.id).deleted_at = datetime.now(timezone.utc)
        comment_replay = await runtime.handle_inbound_comment(reply, SimpleNamespace())
        assert comment_replay.status == 'skipped'
        with db.immediate_session() as session:
            session.get(LocalCommunityThread, source_post.id).deleted_at = datetime.now(timezone.utc)
        post_replay = await runtime.handle_inbound_post(post, SimpleNamespace())
        assert post_replay.status == 'skipped'
        assert tuple(adapter.writes) == prior
        assert relays == ['post', 'comment']
    finally:
        db.close()
