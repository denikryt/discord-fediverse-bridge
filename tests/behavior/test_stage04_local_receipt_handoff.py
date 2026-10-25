"""Domain fan-out regressions for typed receipts and missing nested parent copies.

The real Local Community fan-out and SQLite coordinator run end-to-end; only
platform transport is replaced by a deterministic fake.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from sqlalchemy import select

from src.content_sync.platform_fanout import PlatformFanoutCoordinator
from src.local_communities.discord_fanout import LocalCommunityDiscordFanout
from src.models import (
    LocalCommunityMessage, LocalCommunityMessageSurface,
    LocalCommunityThreadSurface, PlatformDeliveryIntent,
)
from src.platform_types import ConnectorRef, MessageRef
from src.runtime_ports import PlatformAdapterPort
from tests.behavior.test_platform_message_fanout_scenarios import (
    _ForumAdapter, _ThreeForumFixture,
)


class _Adapter(_ForumAdapter):
    """Meet the structural production adapter port without calling Discord."""

    connector = ConnectorRef('discord-primary', 'discord')

    async def fetch_channel(self, channel):
        """No metadata fetch is needed for the verified SQLite surfaces."""
        raise AssertionError('unexpected network lookup')

    async def fetch_thread(self, thread):
        """No thread read is needed to send a verified new target."""
        raise AssertionError('unexpected network lookup')

    async def fetch_message(self, message):
        """No message read is needed for these Create scenarios."""
        raise AssertionError('unexpected network lookup')

    async def reconcile_create(self, attempt):
        """Never fabricate a receipt for an uncertain physical Create."""
        return None

    async def check_permissions(self, principal, channel, actions):
        """These scenarios use the currently authorized SQLite surface claim."""
        return {action: 'allowed' for action in actions}

    async def capabilities(self, channel):
        """Capability discovery is not used in these two Create paths."""
        raise AssertionError('unexpected discovery')

    async def create_reply_thread(self, root, *, title, attempt, dispatch_permit=None):
        """Reply-thread creation is outside forum comment fan-out."""
        raise AssertionError('unexpected reply-thread creation')

    async def edit_message(self, message, content):
        """Mutation transport is outside Create fan-out."""
        raise AssertionError('unexpected edit')

    async def delete_message(self, message):
        """Mutation transport is outside Create fan-out."""
        raise AssertionError('unexpected delete')


class LocalReceiptHandoffScenarios(_ThreeForumFixture):
    """Exercise real Local Community handlers after coordinator-created copies."""

    def setUp(self):
        """Create live forum B as source, with host A and subscriber C recipients."""
        super().setUp()
        self.adapter = _Adapter()
        self.assertTrue(isinstance(self.adapter, PlatformAdapterPort))
        self.fanout = LocalCommunityDiscordFanout(
            database=self.database, bot=None,
            policy_service=SimpleNamespace(is_discord_guild_allowed=lambda guild: guild == 1),
        )
        self.community = self.database.local_communities.get_local_community_by_slug('multi')

    def _create_forum_copies(self):
        """Use domain post handler to create and persist two typed physical roots."""
        result = asyncio.run(self.fanout.fanout_normalized_thread(
            adapter=self.adapter, local_community=self.community,
            thread_row=self.database.local_community_content.get_local_community_thread_by_id(self.canonical_id),
            title='Published', content='hello', author_display_name='Alice',
            source_forum_channel_id=200, include_host=True,
        ))
        return result

    def _new_comment(self, *, ap_id, parent_id, body):
        """Commit one authentic canonical child before performing physical fan-out."""
        snapshot = json.dumps({
            'schemaVersion': 1, 'kind': 'comment', 'apObjectId': ap_id,
            'bodyMarkdown': body, 'authorActorId': 'https://remote.test/actors/alice',
            'authorName': 'Alice', 'url': 'https://remote.test/comment/1',
        }, sort_keys=True)
        with self.database.immediate_session() as session:
            row = LocalCommunityMessage(
                local_community_thread_id=self.canonical_id,
                ap_activity_id=ap_id + '/create', ap_object_id=ap_id,
                parent_ap_object_id=parent_id, direction='inbound',
                current_content_json=snapshot,
            )
            session.add(row)
            session.flush()
            return row.id

    def test_new_coordinator_thread_receipts_are_used_by_comment_fanout(self):
        """Two typed receipts with NULL Discord projections still admit comments."""
        roots = self._create_forum_copies()
        self.assertEqual((roots.delivered, roots.failed), (2, 0))
        with self.database.session() as session:
            copies = session.scalars(select(LocalCommunityThreadSurface).where(
                LocalCommunityThreadSurface.local_community_thread_id == self.canonical_id,
            )).all()
            self.assertEqual({copy.external_thread_id for copy in copies},
                             {'created-100', 'created-300'})
            self.assertTrue(all(copy.discord_thread_id is None for copy in copies))
        ap_id = 'https://remote.test/comment/1'
        child_id = self._new_comment(ap_id=ap_id,
                                     parent_id='https://bridge.test/objects/source', body='one')
        child = self.database.local_community_content.get_local_community_message_by_ap_object_id(ap_id)
        thread = self.database.local_community_content.get_local_community_thread_by_id(self.canonical_id)
        result = asyncio.run(self.fanout.fanout_normalized_message(
            adapter=self.adapter, local_community=self.community,
            thread_row=thread, message_row=child, content='one',
            author_display_name='Alice', source_forum_channel_id=200, include_host=True,
        ))
        self.assertEqual((result.delivered, result.skipped_missing_thread_surface), (2, 0))
        self.assertEqual({(dest, parent) for dest, parent in self.adapter.comments},
                         {('created-100', 'starter-created-100'),
                          ('created-300', 'starter-created-300')})
        with self.database.session() as session:
            self.assertEqual(session.query(LocalCommunityMessageSurface).filter_by(
                local_community_message_id=child_id).count(), 2)

    def test_missing_nested_parent_is_durable_and_wakes_on_receipt(self):
        """A missing parent in C reserves blocked child before any physical POST."""
        self.assertEqual(self._create_forum_copies().delivered, 2)
        thread = self.database.local_community_content.get_local_community_thread_by_id(self.canonical_id)
        parent_ap = 'https://remote.test/comment/parent'
        parent_id = self._new_comment(ap_id=parent_ap, parent_id=thread.ap_object_id, body='parent')
        child_ap = 'https://remote.test/comment/child'
        child_id = self._new_comment(ap_id=child_ap, parent_id=parent_ap, body='child')
        with self.database.immediate_session() as session:
            root_a = session.scalar(select(LocalCommunityThreadSurface).where(
                LocalCommunityThreadSurface.local_community_thread_id == self.canonical_id,
                LocalCommunityThreadSurface.channel_surface_id == self.targets['A'][2]))
            session.add(LocalCommunityMessageSurface(
                connector_id=root_a.connector_id, channel_surface_id=root_a.channel_surface_id,
                local_community_thread_surface_id=root_a.id,
                local_community_message_id=parent_id,
                external_message_id='parent-A',
                external_parent_message_id=root_a.external_starter_message_id,
                role='host',
            ))
        child = self.database.local_community_content.get_local_community_message_by_ap_object_id(child_ap)
        result = asyncio.run(self.fanout.fanout_normalized_message(
            adapter=self.adapter, local_community=self.community,
            thread_row=thread, message_row=child, content='child',
            author_display_name='Alice', source_forum_channel_id=200, include_host=True,
        ))
        self.assertEqual(result.delivered, 1)
        with self.database.session() as session:
            target_c = session.scalar(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.domain == 'local',
                PlatformDeliveryIntent.object_kind == 'comment',
                PlatformDeliveryIntent.canonical_row_id == child_id,
                PlatformDeliveryIntent.channel_surface_id == self.targets['C'][2]))
            self.assertIsNotNone(target_c)
            self.assertEqual(target_c.state, 'blocked')
            self.assertIsNone(target_c.request_json)
            self.assertEqual(target_c.blocked_parent_canonical_id, parent_id)
        self.assertEqual(self.adapter.comments, [('created-100', 'parent-A')])
        # A verified parent copy in C, committed by a different delivery,
        # is the only event that can release this child's blocked intent.
        with self.database.immediate_session() as session:
            root_c = session.scalar(select(LocalCommunityThreadSurface).where(
                LocalCommunityThreadSurface.channel_surface_id == self.targets['C'][2],
                LocalCommunityThreadSurface.local_community_thread_id == self.canonical_id))
            session.add(LocalCommunityMessageSurface(
                connector_id=root_c.connector_id, channel_surface_id=root_c.channel_surface_id,
                local_community_thread_surface_id=root_c.id,
                local_community_message_id=parent_id,
                external_message_id='parent-C',
                external_parent_message_id=root_c.external_starter_message_id,
                role='local_subscriber', local_subscriber_id=self.targets['C'][1],
            ))
        wake = PlatformFanoutCoordinator.from_adapter(self.database, self.adapter)
        asyncio.run(wake._wake_blocked_children())
        self.assertEqual(set(self.adapter.comments),
                         {('created-100', 'parent-A'), ('created-300', 'parent-C')})
        with self.database.session() as session:
            row = session.scalar(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.domain == 'local',
                PlatformDeliveryIntent.object_kind == 'comment',
                PlatformDeliveryIntent.canonical_row_id == child_id,
                PlatformDeliveryIntent.channel_surface_id == self.targets['C'][2]))
            self.assertEqual(row.state, 'delivered')

class LocalReceiptNegativeScenarios(LocalReceiptHandoffScenarios):
    """A missing physical parent is only recoverable for a valid canonical parent."""

    def test_foreign_canonical_parent_has_no_intent_or_network(self):
        """A child cannot adopt an unknown/cross-thread AP parent as blocked work."""
        self.assertEqual(self._create_forum_copies().delivered, 2)
        wrong_ap = 'https://remote.test/wrong/parent'
        child_id = self._new_comment(
            ap_id='https://remote.test/wrong/child', parent_id=wrong_ap, body='bad')
        child = self.database.local_community_content.get_local_community_message_by_ap_object_id(
            'https://remote.test/wrong/child')
        thread = self.database.local_community_content.get_local_community_thread_by_id(self.canonical_id)
        result = asyncio.run(self.fanout.fanout_normalized_message(
            adapter=self.adapter, local_community=self.community,
            thread_row=thread, message_row=child, content='bad',
            author_display_name='Alice', source_forum_channel_id=200, include_host=True,
        ))
        self.assertEqual(result.delivered, 0)
        self.assertEqual(result.failed, 2)
        self.assertEqual(self.adapter.comments, [])
        with self.database.session() as session:
            self.assertEqual(session.scalars(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.canonical_row_id == child_id,
                PlatformDeliveryIntent.object_kind == 'comment',
            )).all(), [])


class MutationRecipientContractScenarios(unittest.TestCase):
    """Mutation rendering must not accept bare recipient URLs as authorization."""

    def test_legacy_direct_url_parameter_is_absent_from_python_apis(self):
        """No Python admission interface silently ignores a dangerous URL list."""
        import inspect
        from src.content_sync.persistence import persist_publish_artifacts
        from src.db.repositories.user_actor_delivery_sources import UserActorDeliverySourceRepository
        from src.db.repositories.user_actor_deliveries import UserActorDeliveryRepository
        from src.runtime_ports import UserActorDeliverySubmitPort
        for entry in (
            persist_publish_artifacts,
            UserActorDeliverySourceRepository.insert_ready_content_source,
            UserActorDeliveryRepository.insert_ready_content_source,
        ):
            with self.subTest(function=entry.__name__):
                self.assertNotIn('direct_target_inbox_urls', inspect.signature(entry).parameters)
        self.assertNotIn('target_inbox_urls', inspect.signature(UserActorDeliverySubmitPort.submit_update).parameters)
        self.assertNotIn('target_inbox_urls', inspect.signature(UserActorDeliverySubmitPort.submit_delete).parameters)

    def test_gateway_mutation_routes_do_not_forward_recipient_urls(self):
        """Fedify builds an activity; SQLite chooses and persists the recipient."""
        from pathlib import Path
        routes = (Path(__file__).resolve().parents[2]
                  / 'fedify-gateway/src/routes/internal-publish.ts').read_text()
        # Only render-only Update/Delete are checked here; an unrelated
        # legacy Local Community Create endpoint has separate semantics.
        mutation_routes = routes.split('app.post("/update",', 1)[1]
        mutation_routes = mutation_routes.split('app.post("/relay",', 1)[0]
        self.assertNotIn('targetInboxUrls', mutation_routes)

class LocalInboundRuntimeReceiptHandoff(unittest.IsolatedAsyncioTestCase):
    """Real inbound AP handlers must consume typed host receipts after physical Create."""

    async def test_remote_post_then_comment_uses_typed_host_thread(self):
        """An inbound post with no numeric copy projection still accepts its reply."""
        import tempfile
        import sys
        from dataclasses import dataclass
        from pathlib import Path
        from types import ModuleType
        from unittest.mock import patch
        from tests.test_local_inbound_adapter_delivery import setup, comment
        from tests.test_inbound_duplicate_admission import _post

        module = ModuleType('src.activitypub_handlers')

        @dataclass
        class HandlerResult:
            """Retain the typed inbound AP outcome without loading Discord SDK."""
            status: str
            outcome: object
            detail: str

        module.HandlerResult = HandlerResult
        with tempfile.TemporaryDirectory() as temp:
            db, runtime, port, _relays = setup(Path(temp))
            try:
                with patch.dict(sys.modules, {'src.activitypub_handlers': module}):
                    post = await runtime.handle_inbound_post(_post(), SimpleNamespace())
                    canonical = db.local_community_content.get_local_community_thread_by_ap_object_id(
                        'https://remote.test/post')
                    root = db.local_community_surfaces.get_host_local_community_thread_surface(canonical.id)
                    self.assertEqual(post.status, 'processed')
                    self.assertEqual((root.external_thread_id, root.external_starter_message_id),
                                     ('400', '401'))
                    self.assertIsNone(root.discord_thread_id)
                    reply = await runtime.handle_inbound_comment(comment(), SimpleNamespace())
                    self.assertEqual(reply.status, 'processed')
                    self.assertIn(('comment', '400', '401'), port.writes)
                    duplicate = await runtime.handle_inbound_comment(comment(), SimpleNamespace())
                    self.assertEqual(duplicate.status, 'skipped')
                    self.assertEqual(len([x for x in port.writes if x[0] == 'comment']), 1)
            finally:
                db.close()
