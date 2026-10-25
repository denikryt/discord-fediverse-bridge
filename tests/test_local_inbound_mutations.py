"""Real-SQLite Local Community AP mutation scenarios at the Platform API boundary.

Only Discord delivery, Fedify relay, and the absent SDK handler-result import are faked.
Canonical objects, policy routing and physical surface mappings use real repositories.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
import sys

import pytest

import discord  # Load the installed SDK; never leave a global placeholder for other suites.
from src.local_communities.runtime import LocalCommunityRuntime
from src.local_communities.service import LocalCommunityService
from src.platform_types import AuthorRef, ContentDocument, MessageRef, MessageView, PlatformOperationError
from support.fake_platform_adapter import FakeDiscordPort
from datetime import datetime, timezone
from src.content_sync.platform_mutations import PlatformPhysicalMutationCoordinator
from src.platform_adapters import PlatformAdapterRegistry
from src.local_communities.inbound_create import LocalCommunityInboundCreateHandler
from support.db import build_database


class PhysicalEdits(FakeDiscordPort):
    """Hold separate real-looking Discord copies and inject per-surface failures."""

    def __init__(self):
        """Start with preserved author headers on two physical surfaces."""
        self.contents = {
            ('1100', '1101'): '`alice`\n\nold post',
            ('2200', '2201'): '`alice`\n\nold post',
            ('1100', '1201'): '`alice`\n\nold comment',
            ('2200', '2301'): '`alice`\n\nold comment',
        }
        self.edits = []
        self.bad_fetch = False
        self.fail_container = None

    async def fetch_message(self, ref):
        """Reject unknown receipts; optionally simulate a wrong foreign message."""
        if self.fail_container == ref.external_container_id:
            raise PlatformOperationError('unavailable', 'network down')
        if self.bad_fetch and ref.external_container_id == '1100':
            return MessageView(MessageRef('discord-primary', '999', ref.external_message_id),
                               content=ContentDocument('`attacker`\n\nforeign'))
        value = self.contents.get((ref.external_container_id, ref.external_message_id))
        if value is None:
            raise PlatformOperationError('not_found', 'no physical receipt')
        return MessageView(ref, author=AuthorRef(ref.connector_key, 'bot', 'bot', 'Bridge', True),
                           content=ContentDocument(value))

    async def edit_message(self, ref, content):
        """Edit only an exact copy, recording observable physical effects."""
        if self.fail_container == ref.external_container_id:
            raise PlatformOperationError('unavailable', 'network down')
        assert ref.connector_key == 'discord-primary'
        assert (ref.external_container_id, ref.external_message_id) in self.contents
        self.edits.append((ref, content.body))
        self.contents[(ref.external_container_id, ref.external_message_id)] = content.body


@pytest.fixture(autouse=True)
def activitypub_sdk_stub(monkeypatch):
    """Keep the SDK import at the outer boundary while exercising the real runtime."""
    module = ModuleType('src.activitypub_handlers')

    @dataclass
    class HandlerResult:
        """Represent the unchanged public outcome contract."""
        status: str
        outcome: object
        detail: str

    module.HandlerResult = HandlerResult
    monkeypatch.setitem(sys.modules, 'src.activitypub_handlers', module)


def world(tmp_path):
    """Create one canonical AP post/comment with host and subscriber copies."""
    db = build_database(tmp_path, 'local-inbound-mutations.sqlite3')
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
    post_id = 'https://remote.example/post/37'
    comment_id = 'https://remote.example/comment/37'
    thread = db.local_community_content.create_local_community_thread(
        local_community_id=community.id,
        discord_thread_id=1100, discord_starter_message_id=1101,
        ap_activity_id='https://remote.example/create/post/37',
        ap_object_id=post_id, direction='ap_to_discord', origin_kind='remote',
    )
    subscriber_thread = db.local_community_surfaces.create_local_community_thread_surface(
        local_community_thread_id=thread.id, discord_forum_channel_id=200,
        discord_thread_id=2200, discord_starter_message_id=2201,
        role='local_subscriber', local_subscriber_id=subscriber.id,
    )
    comment = db.local_community_content.create_local_community_message(
        local_community_thread_id=thread.id, discord_message_id=1201,
        ap_activity_id='https://remote.example/create/comment/37',
        ap_object_id=comment_id, parent_ap_object_id=post_id,
        parent_discord_message_id=1101, direction='ap_to_discord',
    )
    db.local_community_surfaces.create_local_community_message_surface(
        local_community_message_id=comment.id,
        local_community_thread_surface_id=subscriber_thread.id,
        discord_forum_channel_id=200, discord_message_id=2301,
        parent_discord_message_id=2201, role='local_subscriber',
        local_subscriber_id=subscriber.id,
    )
    policy = SimpleNamespace(is_discord_guild_allowed=lambda guild: guild == 10)
    runtime = LocalCommunityRuntime(
        database=db, fedify_gateway=object(), content_publish_service=object(),
        bridge_prefix='[bridge]', bridge_policy_service=policy,
    )
    relay = []

    async def fake_relay(**kwargs):
        """Record actual AP relay decisions while avoiding external HTTP."""
        relay.append(kwargs['operation'])

    runtime.federation_fanout.relay_update_or_delete = fake_relay
    # The historical fixture has actual AP content/actor provenance: Stage 04
    # rejects updates/deletes when those facts are not persisted on the owning
    # canonical row, regardless of the existence of physical Discord copies.
    with db.immediate_session() as session:
        from src.models import LocalCommunityThread, LocalCommunityMessage
        for kind, model, row_id, ap_id in (
            ('post', LocalCommunityThread, thread.id, post_id),
            ('comment', LocalCommunityMessage, comment.id, comment_id),
        ):
            create = event(community.actor_url, ap_id, 'old body')
            session.get(model, row_id).current_content_json = (
                LocalCommunityInboundCreateHandler._inbound_snapshot(create, kind=kind)
            )
    physical = PhysicalEdits()
    registry = PlatformAdapterRegistry((physical.connector,))
    registry.register(physical)
    def allowed_surface(surface_id):
        """Check current guild permission for one exact persisted destination."""
        from src.models import ChannelSurface
        with db.session() as session:
            location = session.get(ChannelSurface, surface_id)
            return bool(location is not None and location.external_guild_id is not None
                        and runtime.surface_fanout.policy_service.is_discord_guild_allowed(
                            int(location.external_guild_id)))
    worker = PlatformPhysicalMutationCoordinator(
        db, registry,
        target_policy=lambda _key, surface_id: allowed_surface(surface_id),
        content_renderer=lambda _domain, _kind, snap: ContentDocument(
            f'`{snap["authorName"]}`\n\n{snap["bodyMarkdown"]}'
        ),
        tombstone_renderer=lambda _domain, _kind: ContentDocument('*deleted by creator*'),
    )
    context = SimpleNamespace(discord_adapter=physical, bot=object(),
                              physical_mutation_coordinator=worker)
    return db, runtime, physical, context, community.actor_url, post_id, comment_id, relay


def event(actor_url, ap_id, body='new body'):
    """Represent a verified inbound ActivityPub mutation of one canonical object."""
    return SimpleNamespace(
        community_actor_id=actor_url, actor_id='https://remote.example/u/alice',
        source_activity_id='https://remote.example/update/' + ap_id.rsplit('/', 1)[-1],
        object=SimpleNamespace(ap_id=ap_id, body_markdown=body,
                               author_name='alice', title='Post', url=ap_id,
                               updated_at=datetime(2026, 1, 5, tzinfo=timezone.utc)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['post', 'comment'])
async def test_ap_update_edits_exact_local_surfaces_through_adapter(tmp_path, kind):
    """Remote Update changes each persisted copy, preserving existing attribution."""
    db, runtime, adapter, context, actor, post, comment, relay = world(tmp_path)
    try:
        action = (runtime.handle_inbound_post_update if kind == 'post'
                  else runtime.handle_inbound_comment_update)
        outcome = await action(event(actor, post if kind == 'post' else comment), context)
        assert outcome.status == 'processed'
        assert len(adapter.edits) == 2
        assert {r.external_container_id for r, _ in adapter.edits} == {'1100', '2200'}
        assert all(body.startswith('`alice`\n\n') for _, body in adapter.edits)
        assert relay == ['update']
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['post', 'comment'])
async def test_ap_delete_marks_physical_copies_without_deleting(tmp_path, kind):
    """Remote Delete replaces exact stored message bodies with tombstones."""
    db, runtime, adapter, context, actor, post, comment, relay = world(tmp_path)
    try:
        action = (runtime.handle_inbound_post_delete if kind == 'post'
                  else runtime.handle_inbound_comment_delete)
        outcome = await action(event(actor, post if kind == 'post' else comment), context)
        assert outcome.status == 'processed'
        assert len(adapter.edits) == 2
        assert {body for _, body in adapter.edits} == {'*deleted by creator*'}
        assert relay == ['delete']
    finally:
        db.close()


@pytest.mark.asyncio
async def test_ap_update_failed_transport_does_not_block_healthy_sibling(tmp_path):
    """One failed typed edit cannot overwrite its copy or suppress healthy targets."""
    db, runtime, adapter, context, actor, post, _, relay = world(tmp_path)
    try:
        adapter.fail_container = '1100'
        outcome = await runtime.handle_inbound_post_update(event(actor, post), context)
        assert outcome.status == 'processed'
        assert len(adapter.edits) == 1
        assert adapter.edits[0][0].external_container_id == '2200'
        assert adapter.contents[('1100', '1101')] == '`alice`\n\nold post'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unmapped_ap_mutation_never_reaches_adapter_or_relay(tmp_path):
    """Unknown canonical object cannot authorize a physical Discord edit."""
    db, runtime, adapter, context, actor, _, _, relay = world(tmp_path)
    try:
        outcome = await runtime.handle_inbound_post_delete(
            event(actor, 'https://remote.example/missing'), context,
        )
        assert outcome.status == 'skipped'
        assert adapter.edits == []
        assert relay == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_one_failed_local_surface_does_not_block_other_destination(tmp_path):
    """One Discord timeout cannot suppress delivery to a healthy subscribed forum."""
    db, runtime, adapter, context, actor, post, _, relay = world(tmp_path)
    try:
        adapter.fail_container = '1100'
        outcome = await runtime.handle_inbound_post_update(event(actor, post), context)
        assert outcome.status == 'processed'
        assert [(ref.external_container_id, ref.external_message_id)
                for ref, _ in adapter.edits] == [('2200', '2201')]
        assert adapter.contents[('1100', '1101')] == '`alice`\n\nold post'
    finally:
        db.close()


@pytest.mark.asyncio
async def test_disallowed_discord_guild_does_not_receive_ap_mutation(tmp_path):
    """A physical destination forbidden by current policy receives no API edit."""
    db, runtime, adapter, context, actor, _, comment, relay = world(tmp_path)
    try:
        runtime.surface_fanout.policy_service.is_discord_guild_allowed = lambda guild: False
        outcome = await runtime.handle_inbound_comment_delete(event(actor, comment), context)
        assert outcome.status == 'processed'
        assert adapter.edits == []
        assert adapter.contents[('1100', '1201')] == '`alice`\n\nold comment'
        assert adapter.contents[('2200', '2301')] == '`alice`\n\nold comment'
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_evidence', ['missing_clock', 'missing_activity', 'foreign_actor'])
async def test_inbound_ap_update_without_authoritative_version_or_owner_has_no_effect(
    tmp_path, invalid_evidence,
):
    """Reject absent revision proof or wrong AP author before SQLite revision and I/O."""
    db, runtime, adapter, context, actor, post_id, _, relay = world(tmp_path)
    try:
        source = event(actor, post_id)
        if invalid_evidence == 'missing_clock':
            source.object.updated_at = None
        elif invalid_evidence == 'missing_activity':
            source.source_activity_id = None
        else:
            source.actor_id = 'https://other.example/u/forged'
        outcome = await runtime.handle_inbound_post_update(source, context)
        assert outcome.status == 'skipped'
        canonical = db.local_community_content.get_local_community_thread_by_ap_object_id(post_id)
        assert canonical.content_revision == 0 and canonical.deleted_at is None
        assert adapter.edits == [] and relay == []
    finally:
        db.close()
