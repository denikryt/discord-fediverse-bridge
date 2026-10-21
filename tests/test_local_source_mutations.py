"""Real SQLite Local Community source edits/deletes through a verified transport.

Gateway requests and physical Discord HTTP are the only external fakes. These
scenarios exercise the actual local mutation runtime and stored surface rows.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

# Import-only stub: the SDK is unavailable in this environment. Nothing in the
# runtime scenario may call an SDK method or rely on this stub for transport.
sys.modules.setdefault('discord', ModuleType('discord'))

from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from src.platform_types import ContentDocument, MessageRef, MessageView, PlatformOperationError
from support.db import build_database


class PhysicalCopies:
    """Model exact Discord thread/message identities and network failures."""

    def __init__(self):
        """Seed host and subscriber copies with independent attribution text."""
        self.contents = {
            ('1100', '1101'): '`alice`\n\noriginal',
            ('2200', '2201'): '`alice`\n\noriginal',
            ('1100', '1201'): '`alice`\n\ncomment',
            ('2200', '2301'): '`alice`\n\ncomment',
        }
        self.edits = []
        self.bad_read = False
        self.fail_container = None

    async def fetch_message(self, ref):
        """Read exact existing physical copy, or simulate an adversarial reply."""
        if self.fail_container == ref.external_container_id:
            raise PlatformOperationError('unavailable', 'network error')
        if self.bad_read and ref.external_container_id == '2200':
            return MessageView(MessageRef('discord-primary', '9999', ref.external_message_id),
                               content=ContentDocument('`attacker`\n\nforeign'))
        body = self.contents.get((ref.external_container_id, ref.external_message_id))
        if body is None:
            raise PlatformOperationError('not_found', 'missing message')
        return MessageView(ref, content=ContentDocument(body))

    async def edit_message(self, ref, content):
        """Edit only one identified copy; record observable network effects."""
        if self.fail_container == ref.external_container_id:
            raise PlatformOperationError('unavailable', 'network error')
        assert (ref.external_container_id, ref.external_message_id) in self.contents
        self.edits.append((ref, content.body))
        self.contents[(ref.external_container_id, ref.external_message_id)] = content.body


class ForbiddenSdk:
    """Prove production mutation requests never fall back to legacy SDK I/O."""

    async def get_thread_by_id(self, *args, **kwargs):
        """Fail on any attempt to reach the legacy Discord transport."""
        raise AssertionError('unexpected legacy SDK fallback')


class Gateway:
    """Record the canonical ActivityPub Update/Delete side effects."""

    def __init__(self):
        """Keep one ordered list of emitted federation requests."""
        self.updates = []
        self.deletes = []

    async def update_content(self, request):
        """Accept one outgoing canonical Update."""
        self.updates.append(request)

    async def delete_content(self, request):
        """Accept one outgoing canonical Delete."""
        self.deletes.append(request)


def world(tmp_path):
    """Seed a canonical host post/comment with one active sibling subscriber."""
    db = build_database(tmp_path, 'local-source-mutations.db')
    LocalCommunityService(
        database=db, base_url='https://bridge.example',
        keypair_generator=lambda: ('public', 'private'),
    ).create_local_community(
        discord_guild_id=10, discord_forum_channel_id=100,
        slug='hackers', name='Hackers', description='Community',
        created_by_discord_user_id='123',
    )
    community = db.local_communities.get_local_community_by_slug('hackers')
    subscriber = db.local_subscribers.create_local_subscriber(
        local_community_id=community.id, discord_guild_id=10,
        discord_channel_id=200, initiated_by_discord_user_id='123',
    )
    post_id = 'https://bridge.example/post/39'
    comment_id = 'https://bridge.example/comment/39'
    thread = db.local_community_content.create_local_community_thread(
        local_community_id=community.id, discord_thread_id=1100,
        discord_starter_message_id=1101,
        ap_activity_id='https://bridge.example/create/post/39',
        ap_object_id=post_id, direction='discord_to_ap', origin_kind='discord_host',
    )
    remote_thread = db.local_community_surfaces.create_local_community_thread_surface(
        local_community_thread_id=thread.id, discord_forum_channel_id=200,
        discord_thread_id=2200, discord_starter_message_id=2201,
        role='local_subscriber', local_subscriber_id=subscriber.id,
    )
    db.local_community_content.create_local_community_message(
        local_community_thread_id=thread.id, discord_message_id=1201,
        ap_activity_id='https://bridge.example/create/comment/39',
        ap_object_id=comment_id, parent_ap_object_id=post_id,
        parent_discord_message_id=1101, direction='discord_to_ap',
    )
    message = db.local_community_content.get_local_community_message_by_ap_object_id(comment_id)
    db.local_community_surfaces.create_local_community_message_surface(
        local_community_message_id=message.id,
        local_community_thread_surface_id=remote_thread.id,
        discord_forum_channel_id=200, discord_message_id=2301,
        parent_discord_message_id=2201, role='local_subscriber',
        local_subscriber_id=subscriber.id,
    )
    for kind, object_id, activity_id, physical_id in (
        ('post', post_id, 'https://bridge.example/create/post/39', 1101),
        ('comment', comment_id, 'https://bridge.example/create/comment/39', 1201),
    ):
        db.activitypub_objects.create_published_activity_object(
            actor_username='alice', actor_url='https://bridge.example/actors/users/alice',
            community_actor_url=community.actor_url, activity_id=activity_id,
            object_id=object_id, kind=kind, title='A post' if kind == 'post' else None,
            body_markdown='original', in_reply_to_object_id=post_id if kind == 'comment' else None,
            discord_channel_id=100, discord_message_id=physical_id,
        )
    policy = SimpleNamespace(
        is_discord_guild_allowed=lambda guild: guild == 10,
        snapshot=lambda: SimpleNamespace(federation_decision=lambda url: SimpleNamespace(allowed=True)),
    )
    gateway = Gateway()
    runtime = LocalCommunityRuntime(
        database=db, fedify_gateway=gateway,
        content_publish_service=object(), bridge_prefix='[bridge]',
        bridge_policy_service=policy,
    )
    # Production binds an SDK-backed bot, but it must never be used for this operation.
    runtime.bind_discord_client(ForbiddenSdk())
    physical = PhysicalCopies()
    context = SimpleNamespace(
        bridge_policy_service=policy, fedify_gateway=gateway,
        user_actor_delivery_service=None,
        user_actor_delivery_wake=SimpleNamespace(wake=lambda: None),
    )
    return db, runtime, physical, context, gateway, subscriber


@pytest.mark.asyncio
@pytest.mark.parametrize('source_id,target_id', [(1101, '2201'), (1201, '2301')])
async def test_host_edit_uses_verified_sibling_copy_and_preserves_header(tmp_path, source_id, target_id):
    """Host authored Update reaches sibling via adapter but never edits the host source."""
    db, runtime, physical, context, gateway, _ = world(tmp_path)
    try:
        runtime.bind_discord_adapter(physical)
        await runtime.handle_discord_message_edit(
            message_id=source_id, new_content='updated', mutation_context=context,
        )
        assert [(ref.external_message_id, body) for ref, body in physical.edits] == [
            (target_id, '`alice`\n\nupdated'),
        ]
        assert physical.contents[('1100', str(source_id))].endswith('original' if source_id == 1101 else 'comment')
        assert len(gateway.updates) == 1
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('source_id,target_id', [(1101, '2201'), (1201, '2301')])
async def test_host_delete_marks_only_sibling_and_preserves_source(tmp_path, source_id, target_id):
    """Local Delete retains visible tombstone and ActivityPub Delete semantics."""
    db, runtime, physical, context, gateway, _ = world(tmp_path)
    try:
        runtime.bind_discord_adapter(physical)
        await runtime.handle_discord_message_delete(message_id=source_id, mutation_context=context)
        assert [(ref.external_message_id, body) for ref, body in physical.edits] == [
            (target_id, '*deleted by creator*'),
        ]
        assert ('1100', str(source_id)) in physical.contents
        assert len(gateway.deletes) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_wrong_receipt_fails_closed_no_sdk_fallback(tmp_path):
    """Adversarial read cannot overwrite a foreign message or retry via SDK."""
    db, runtime, physical, context, gateway, _ = world(tmp_path)
    try:
        runtime.bind_discord_adapter(physical)
        physical.bad_read = True
        await runtime.handle_discord_message_edit(
            message_id=1101, new_content='updated', mutation_context=context,
        )
        assert physical.edits == []
        assert physical.contents[('2200', '2201')] == '`alice`\n\noriginal'
        assert len(gateway.updates) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_revoked_subscriber_cannot_be_mutated(tmp_path):
    """Saved physical receipt is insufficient after Undo of subscriber membership."""
    db, runtime, physical, context, gateway, subscriber = world(tmp_path)
    try:
        runtime.bind_discord_adapter(physical)
        with db.session() as session:
            from src.models import LocalSubscriber
            session.get(LocalSubscriber, subscriber.id).status = 'removed'
        await runtime.handle_discord_message_delete(message_id=1101, mutation_context=context)
        assert physical.edits == []
        assert len(gateway.deletes) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_transport_failure_never_uses_sdk_fallback(tmp_path):
    """A failed external adapter edit must not invoke legacy SDK as retry."""
    db, runtime, physical, context, gateway, _ = world(tmp_path)
    try:
        runtime.bind_discord_adapter(physical)
        physical.fail_container = '2200'
        await runtime.handle_discord_message_edit(
            message_id=1101, new_content='updated', mutation_context=context,
        )
        assert physical.edits == []
        assert len(gateway.updates) == 1
    finally:
        db.close()

@pytest.mark.asyncio
async def test_subscriber_authored_edit_only_changes_host_surface(tmp_path):
    """An active subscriber can mutate canonical content but not its source copy."""
    db, runtime, physical, context, gateway, _ = world(tmp_path)
    try:
        runtime.bind_discord_adapter(physical)
        await runtime.handle_discord_message_edit(
            message_id=2201, new_content='subscriber update', mutation_context=context,
        )
        assert [(ref.external_message_id, body) for ref, body in physical.edits] == [
            ('1101', '`alice`\n\nsubscriber update'),
        ]
        assert physical.contents[('2200', '2201')] == '`alice`\n\noriginal'
        assert len(gateway.updates) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_revoked_subscriber_origin_cannot_publish_or_mutate_host(tmp_path):
    """An inactive source subscriber cannot issue AP Update/Delete or fan out."""
    db, runtime, physical, context, gateway, subscriber = world(tmp_path)
    try:
        runtime.bind_discord_adapter(physical)
        with db.session() as session:
            from src.models import LocalSubscriber
            session.get(LocalSubscriber, subscriber.id).status = 'removed'
        await runtime.handle_discord_message_edit(
            message_id=2201, new_content='unauthorized', mutation_context=context,
        )
        await runtime.handle_discord_message_delete(
            message_id=2301, mutation_context=context,
        )
        assert gateway.updates == [] and gateway.deletes == []
        assert physical.edits == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_one_failed_subscriber_does_not_block_another(tmp_path):
    """Existing SQLite surface routing isolates an offline forum from healthy copies."""
    db, runtime, physical, context, gateway, _ = world(tmp_path)
    try:
        runtime.bind_discord_adapter(physical)
        community = db.local_communities.get_local_community_by_slug('hackers')
        other = db.local_subscribers.create_local_subscriber(
            local_community_id=community.id, discord_guild_id=10,
            discord_channel_id=300, initiated_by_discord_user_id='123',
        )
        thread = db.local_community_content.get_local_community_thread_by_ap_object_id(
            'https://bridge.example/post/39',
        )
        db.local_community_surfaces.create_local_community_thread_surface(
            local_community_thread_id=thread.id, discord_forum_channel_id=300,
            discord_thread_id=3300, discord_starter_message_id=3301,
            role='local_subscriber', local_subscriber_id=other.id,
        )
        physical.contents[('3300', '3301')] = '`alice`\n\noriginal'
        physical.fail_container = '2200'
        await runtime.handle_discord_message_edit(
            message_id=1101, new_content='updated', mutation_context=context,
        )
        assert [(ref.external_container_id, body) for ref, body in physical.edits] == [
            ('3300', '`alice`\n\nupdated'),
        ]
        assert len(gateway.updates) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_adapter_cannot_be_rebound_after_startup(tmp_path):
    """Changing the live Discord connector silently must not redirect mutations."""
    db, runtime, physical, _, _, _ = world(tmp_path)
    try:
        runtime.bind_discord_adapter(physical)
        with pytest.raises(RuntimeError, match='already configured'):
            runtime.bind_discord_adapter(PhysicalCopies())
    finally:
        db.close()
