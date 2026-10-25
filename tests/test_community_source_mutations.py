"""Community source edit/delete scenarios through real SQLite and typed physical delivery.

The bridge-owned copies are stored as real CommunityMessageGroupDelivery rows.
Only Discord network I/O and legacy SDK import are replaced in this test.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

import discord  # Load the installed SDK; never leave a global placeholder for other suites.

from src.community_sync.discord_fanout import DiscordFanout
from src.community_sync.runtime import CommunityRuntime
from src.db import Database
from src.platform_types import ContentDocument, MessageRef, MessageView, PlatformOperationError
from src.runtime_ports import DiscordMutationContext


ACTOR = 'https://example.net/c/community'


class PhysicalCopies:
    """Exercise exact physical reads/edits/deletes without making SDK calls."""

    def __init__(self):
        """Store one source, two mirror copies and an inbound non-source copy."""
        self.messages = {
            ('200', '300'): '`alice`\n\nsource',
            ('201', '301'): '`alice`\n\nold',
            ('202', '302'): '`alice`\n\nold',
            ('203', '303'): '`remote`\n\nold',
        }
        self.edits = []
        self.deletes = []
        self.bad_fetch = False
        self.fail_target = None

    async def fetch_message(self, ref):
        """Read exactly the requested message, with optional malicious mismatch."""
        if self.fail_target == ref.external_message_id:
            raise PlatformOperationError('unavailable', 'simulated Discord failure')
        if self.bad_fetch and ref.external_message_id == '301':
            return MessageView(MessageRef(ref.connector_key, '9999', ref.external_message_id),
                               content=ContentDocument('`foreign`\n\ncontent'))
        body = self.messages.get((ref.external_container_id, ref.external_message_id))
        if body is None:
            raise PlatformOperationError('not_found', 'unknown message')
        return MessageView(ref, content=ContentDocument(body))

    async def edit_message(self, ref, content):
        """Persist the visible edit and record its exact physical destination."""
        if self.fail_target == ref.external_message_id:
            raise PlatformOperationError('unavailable', 'simulated Discord failure')
        assert (ref.external_container_id, ref.external_message_id) in self.messages
        self.edits.append((ref, content.body))
        self.messages[(ref.external_container_id, ref.external_message_id)] = content.body

    async def delete_message(self, ref):
        """Remove one exact message without removing its physical thread."""
        if self.fail_target == ref.external_message_id:
            raise PlatformOperationError('unavailable', 'simulated Discord failure')
        assert (ref.external_container_id, ref.external_message_id) in self.messages
        self.deletes.append(ref)
        del self.messages[(ref.external_container_id, ref.external_message_id)]


class Tracker:
    """Track bridge-originated mutations for echo suppression."""

    def __init__(self):
        """Start with no tracked gateway echoes."""
        self.edits = []
        self.deletes = []

    def track_message_edit(self, message_id):
        """Remember one confirmed physical edit."""
        self.edits.append(message_id)

    def track_message_delete(self, message_id):
        """Remember one confirmed physical delete."""
        self.deletes.append(message_id)


def world(tmp_path):
    """Create canonical source/mirror/inbound mappings and their subscriptions."""
    database = Database(f'sqlite:///{tmp_path / "source-mutations.db"}')
    database.migrate()
    for channel_id in (100, 101, 102, 103):
        database.remote_subscriptions.create_subscription(
            discord_channel_id=channel_id, discord_guild_id=1,
            lemmy_community_actor_id=ACTOR, lemmy_community_name='community',
            lemmy_community_id=44, status='accepted',
        )
    # The source mapping and all delivery rows belong to one canonical message.
    group = database.discord_fanout_groups.create_message_group(
        community_actor_id=ACTOR, thread_group_id=1,
        source_channel_id=100, source_thread_id=200, source_message_id=300,
    )
    for channel_id, thread_id, message_id, role in (
        (100, 200, 300, 'source'), (101, 201, 301, 'mirror'),
        (102, 202, 302, 'mirror'), (103, 203, 303, 'inbound'),
    ):
        database.discord_fanout_groups.add_message_delivery(
            message_group_id=group.id, discord_channel_id=channel_id,
            discord_thread_id=thread_id, discord_message_id=message_id, role=role,
        )
    policy = SimpleNamespace(
        is_discord_guild_allowed=lambda guild: guild == 1,
        federation_decision=lambda actor: SimpleNamespace(allowed=True),
    )
    adapter = PhysicalCopies()
    tracker = Tracker()

    class ForbiddenSdk:
        """Legacy client must never be used when a typed adapter is bound."""

        async def get_thread_by_id(self, thread_id):
            """Reject SDK access even if the typed operation failed."""
            raise AssertionError('unexpected legacy SDK fallback')

    fanout = DiscordFanout(
        bot=ForbiddenSdk(), mutation_tracker=tracker,
        database=database, policy_service=policy, adapter=adapter,
    )
    runtime = CommunityRuntime(database=database, content_publish_service=object(), discord_fanout=fanout)
    context = DiscordMutationContext(
        bridge_policy_service=policy, user_actor_delivery_service=None,
        user_actor_delivery_wake=SimpleNamespace(wake=lambda: None), fedify_gateway=None,
    )
    return database, runtime, adapter, tracker, context


@pytest.mark.asyncio
async def test_source_edit_uses_typed_physical_copies_preserves_header_and_excludes_source(tmp_path):
    """Source edit updates only mirror copies, preserving each persisted author header."""
    db, runtime, adapter, tracker, context = world(tmp_path)
    try:
        await runtime.handle_discord_message_edit(
            300, 'updated', mutation_context=context, author_display_name='alice',
        )
        assert {ref.external_message_id for ref, _ in adapter.edits} == {'301', '302'}
        assert {body for _, body in adapter.edits} == {'`alice`\n\nupdated'}
        assert adapter.messages[('200', '300')] == '`alice`\n\nsource'
        assert adapter.messages[('203', '303')] == '`remote`\n\nold'
        assert set(tracker.edits) == {301, 302}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_source_delete_deletes_only_non_source_deliveries_through_adapter(tmp_path):
    """Source delete removes persisted non-source copies but never deletes source."""
    db, runtime, adapter, tracker, context = world(tmp_path)
    try:
        await runtime.handle_discord_message_delete(300, mutation_context=context)
        assert {ref.external_message_id for ref in adapter.deletes} == {'301', '302', '303'}
        assert set(tracker.deletes) == {301, 302, 303}
        assert ('200', '300') in adapter.messages
    finally:
        db.close()


@pytest.mark.asyncio
async def test_invalid_read_and_failure_do_not_overwrite_foreign_or_block_other_copies(tmp_path):
    """Wrong physical fetch receipt and failed target cannot cause a second or foreign edit."""
    db, runtime, adapter, tracker, context = world(tmp_path)
    try:
        adapter.bad_fetch = True
        await runtime.handle_discord_message_edit(300, 'edited', mutation_context=context)
        assert [ref.external_message_id for ref, _ in adapter.edits] == ['302']
        assert tracker.edits == [302]
        adapter.edits.clear()
        tracker.edits.clear()
        adapter.bad_fetch = False
        adapter.fail_target = '301'
        await runtime.handle_discord_message_edit(300, 'again', mutation_context=context)
        assert [ref.external_message_id for ref, _ in adapter.edits] == ['302']
        assert tracker.edits == [302]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_revoked_mirror_subscription_prevents_new_mutation(tmp_path):
    """After unsubscribe, historical mirror mapping does not grant new edit/delete rights."""
    db, runtime, adapter, tracker, context = world(tmp_path)
    try:
        subscription = db.remote_subscriptions.get_subscription_by_channel(101)
        # Revoked claim remains in SQLite so old physical receipts still exist.
        with db.session() as session:
            from src.models import ChannelCommunitySubscription
            row = session.get(ChannelCommunitySubscription, subscription.id)
            row.status = 'removed'
        await runtime.handle_discord_message_edit(300, 'new', mutation_context=context)
        assert {ref.external_message_id for ref, _ in adapter.edits} == {'302'}
        assert 301 not in tracker.edits
    finally:
        db.close()


@pytest.mark.asyncio
async def test_community_actor_mismatch_prevents_mutating_foreign_forum(tmp_path):
    """A forum newly bound to another community cannot receive an old group's edit."""
    db, runtime, adapter, tracker, context = world(tmp_path)
    try:
        subscription = db.remote_subscriptions.get_subscription_by_channel(101)
        with db.session() as session:
            from src.models import ChannelCommunitySubscription
            row = session.get(ChannelCommunitySubscription, subscription.id)
            row.lemmy_community_actor_id = 'https://other.example/c/foreign'
        await runtime.handle_discord_message_edit(300, 'new', mutation_context=context)
        assert {ref.external_message_id for ref, _ in adapter.edits} == {'302'}
        assert 301 not in tracker.edits
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delete_transport_failure_isolated_no_sdk_retry(tmp_path):
    """A failed physical delete does not prevent healthy mirrors or touch source."""
    db, runtime, adapter, tracker, context = world(tmp_path)
    try:
        adapter.fail_target = '301'
        await runtime.handle_discord_message_delete(300, mutation_context=context)
        assert {ref.external_message_id for ref in adapter.deletes} == {'302', '303'}
        assert set(tracker.deletes) == {302, 303}
        assert ('201', '301') in adapter.messages
        assert ('200', '300') in adapter.messages
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delete_refuses_revoked_and_rebound_destinations(tmp_path):
    """Current subscription actor and status, not old copies, authorize deletes."""
    db, runtime, adapter, tracker, context = world(tmp_path)
    try:
        with db.session() as session:
            from src.models import ChannelCommunitySubscription
            removed = db.remote_subscriptions.get_subscription_by_channel(101)
            changed = db.remote_subscriptions.get_subscription_by_channel(102)
            session.get(ChannelCommunitySubscription, removed.id).status = 'removed'
            session.get(ChannelCommunitySubscription, changed.id).lemmy_community_actor_id = 'https://other.example/c/foreign'
        await runtime.handle_discord_message_delete(300, mutation_context=context)
        assert [ref.external_message_id for ref in adapter.deletes] == ['303']
        assert tracker.deletes == [303]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_invalid_guild_claim_skips_mirror_mutation(tmp_path):
    """Invalid guild metadata cannot authorize physical edits even with saved receipts."""
    db, runtime, adapter, tracker, context = world(tmp_path)
    try:
        subscription = db.remote_subscriptions.get_subscription_by_channel(101)
        with db.session() as session:
            from src.models import ChannelCommunitySubscription
            session.get(ChannelCommunitySubscription, subscription.id).discord_guild_id = None
        await runtime.handle_discord_message_edit(300, 'new', mutation_context=context)
        assert {ref.external_message_id for ref, _ in adapter.edits} == {'302'}
        assert tracker.edits == [302]
    finally:
        db.close()
