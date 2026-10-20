"""Local Community AP Create reaches real SQLite receipts through the platform boundary."""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
import sys

import pytest

from src.platform_types import ChannelRef, CreatedMessage, CreatedThread, MessageRef, ThreadRef
from src.local_communities.runtime import LocalCommunityRuntime
from test_stage03_local_receipt_persistence import world
from test_stage03_inbound_duplicate_admission import _post, _accepted


class Delivery:
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
        community_actor_id='https://bridge.test/c/hackers', actor_id=author,
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
        with pytest.raises(Exception):
            await runtime.handle_inbound_post(_post(), SimpleNamespace())
        assert db.local_community_content.get_local_community_thread_by_ap_object_id('https://remote.test/post') is None
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
        # Only the previously failed subscriber is attempted again.
        assert [forum for operation, forum, _ in adapter.writes if operation == 'post'].count('300') == 1
        assert [forum for operation, forum, _ in adapter.writes if operation == 'post'].count('200') == 1
        assert [forum for operation, forum, _ in adapter.writes if operation == 'post'].count('201') == 2
        row = db.local_community_content.get_local_community_thread_by_ap_object_id('https://remote.test/post')
        assert db.local_community_surfaces.get_local_community_thread_surface(
            local_community_thread_id=row.id, discord_forum_channel_id=201,
        ).discord_thread_id == 600
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
        with pytest.raises(Exception):
            await runtime.handle_inbound_post(_post(), SimpleNamespace())
        assert db.local_community_content.get_local_community_thread_by_ap_object_id('https://remote.test/post') is None
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
