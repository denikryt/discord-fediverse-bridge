"""Authenticated SDK READY → Node RPC → Python bot-owned physical mutation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import unittest

from sqlalchemy import select

from src.content_sync.platform_mutations import PlatformPhysicalMutationCoordinator
from src.db.repositories.platform_surfaces import PlatformSurfaceRepository
from src.models import (ChannelSurface, LocalCommunityThread,
                        LocalCommunityThreadSurface, PlatformConnector)
from tests import test_physical_mutation_recovery as mutation_fixture
from src.fluxer_adapter import FluxerAdapter
from src.fluxer_connector_config import FluxerConnectorConfig
from src.fluxer_rpc_client import FluxerRpcClient
from src.platform_types import ContentDocument, MessageRef, PlatformOperationError


class FluxerVerifiedBotOwnershipScenarios(unittest.IsolatedAsyncioTestCase):
    """Prove bot mutation ownership through two running processes and remote state."""

    def setUp(self) -> None:
        """Seed the same real SQLite durable copy fixture used by Discord regression."""
        mutation_fixture.PhysicalMutationRecoveryScenarios.setUp(self)

    async def asyncSetUp(self) -> None:
        """Start Node service with a real READY/health owner and fake SDK network edge."""
        root = Path(__file__).resolve().parents[2]
        self.node = await asyncio.create_subprocess_exec(
            'node', '--experimental-strip-types',
            str(root / 'fluxer-connector/tests/fixture_sdk_owned_messages.mjs'),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        assert self.node.stdout is not None
        port = await asyncio.wait_for(self.node.stdout.readline(), timeout=10)
        if not port.strip().isdigit():
            stderr = await self.node.stderr.read() if self.node.stderr else b''
            self.fail(f'Node service did not authenticate: {stderr[:300]!r}')
        config = FluxerConnectorConfig('fx-owned', 'https://fx.test',
                                       f'http://127.0.0.1:{int(port)}',
                                       'test-transport-secret')
        self.rpc = FluxerRpcClient(config)
        # Deliberately pass no Python bot_actor_id: only READY health can bind it.
        self.adapter = FluxerAdapter(connector_key='fx-owned',
                                     instance_origin='https://fx.test', rpc=self.rpc)

    async def asyncTearDown(self) -> None:
        """Join client/sidecar before another test can reuse any RPC resources."""
        await self.rpc.close()
        if self.node.returncode is None:
            self.node.terminate()
        await asyncio.wait_for(self.node.wait(), timeout=10)

    def _fluxer_mutation_coordinator(self, message_id: str):
        """Replace only this test copy's transport placement with a Fluxer owner."""
        with self.db.immediate_session() as session:
            connector = PlatformSurfaceRepository.add_connector(
                session, connector_key='fx-owned', platform='fluxer',
                instance_origin='https://fx.test',
            )
            session.flush()  # Materialize the connector FK before reassigning this test surface.
            original = session.get(ChannelSurface, self.surface_id)
            community_id = original.local_community_id
            # Connector/surface composite FKs prevent a cross-connector
            # in-place rebind: create a distinct test-only Fluxer placement.
            original.local_community_id = None
            session.flush()
            surface = ChannelSurface(
                connector_id=connector.id, external_channel_id='forum-1',
                external_guild_id='guild-1', kind='forum',
                local_community_id=community_id,
            )
            session.add(surface)
            session.flush()
            self.surface_id = surface.id
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            copy.connector_id = connector.id
            copy.channel_surface_id = surface.id
            copy.external_thread_id = 'thread-1'
            copy.external_starter_message_id = message_id
            copy.discord_forum_channel_id = None
            copy.discord_thread_id = None
            copy.discord_starter_message_id = None

        class Registry:
            """Allow the one attested Fluxer adapter and no Discord fallback."""

            def get(inner, key):
                """Refuse unregistered transport identities before dispatch."""
                if key != 'fx-owned':
                    raise LookupError('wrong physical connector')
                return self.adapter

        return PlatformPhysicalMutationCoordinator(
            self.db, Registry(),
            target_policy=lambda key, surface_id: key == 'fx-owned'
                                           and surface_id == self.surface_id,
            content_renderer=lambda _domain, _kind, data: ContentDocument(data['bodyMarkdown']),
        )

    async def test_durable_sqlite_mutation_tracks_real_node_bot_edit_delete(self) -> None:
        """Canonical revision → CAS claim → authenticated RPC write → applied revision."""
        worker = self._fluxer_mutation_coordinator('owned')
        self.assertEqual(await worker.drain(connector_key='fx-owned'), 1)
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.applied_revision, 1)
            self.assertEqual(copy.mutation_state, 'idle')
        actual = await self.adapter.fetch_message(MessageRef('fx-owned', 'thread-1', 'owned'))
        self.assertEqual(actual.content.body, 'latest')
        with self.db.immediate_session() as session:
            post = session.get(LocalCommunityThread, self.post_id)
            post.deleted_at = datetime.now(timezone.utc)
            post.content_revision = 2
        self.assertEqual(await worker.drain(connector_key='fx-owned'), 1)
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertEqual(copy.applied_revision, 2)
            self.assertEqual(copy.mutation_state, 'idle')
        with self.assertRaises(PlatformOperationError):
            await self.adapter.fetch_message(MessageRef('fx-owned', 'thread-1', 'owned'))

    async def test_durable_sqlite_foreign_bot_refusal_has_no_claim_or_remote_edit(self) -> None:
        """A foreign bot copy fails read-only preflight, not physical mutation."""
        worker = self._fluxer_mutation_coordinator('foreign')
        self.assertEqual(await worker.drain(connector_key='fx-owned'), 0)
        with self.db.session() as session:
            copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            self.assertIsNone(copy.applied_revision)
            self.assertIsNone(copy.mutation_claim_token)
            self.assertEqual(copy.mutation_state, 'failed_definite')
        actual = await self.adapter.fetch_message(MessageRef('fx-owned', 'thread-1', 'foreign'))
        self.assertEqual(actual.content.body, 'other bot')

    async def test_authenticated_bot_edit_delete_exact_copy(self) -> None:
        """A bot-created message is changed/deleted, not its sibling or forum."""
        own = MessageRef('fx-owned', 'thread-1', 'owned')
        before = await self.adapter.fetch_message(own)
        self.assertTrue(before.author.is_bridge_owned)
        self.assertEqual(self.adapter._bot_id, 'bot-one')
        await self.adapter.edit_message(own, ContentDocument('edited'))
        updated = await self.adapter.fetch_message(own)
        self.assertEqual(updated.content.body, 'edited')
        await self.adapter.delete_message(own)
        with self.assertRaises(PlatformOperationError) as missing:
            await self.adapter.fetch_message(own)
        self.assertEqual(missing.exception.reason, 'not_found')
        foreign = await self.adapter.fetch_message(MessageRef('fx-owned', 'thread-1', 'foreign'))
        self.assertEqual(foreign.content.body, 'other bot')

    async def test_foreign_bot_and_webhook_same_actor_cannot_mutate(self) -> None:
        """Current READY bot ID alone cannot promote another bot or webhook echo."""
        for mid in ('foreign', 'webhook'):
            with self.subTest(message=mid):
                ref = MessageRef('fx-owned', 'thread-1', mid)
                found = await self.adapter.fetch_message(ref)
                self.assertFalse(found.author.is_bridge_owned)
                with self.assertRaises(PlatformOperationError) as refused:
                    await self.adapter.delete_message(ref)
                self.assertEqual(refused.exception.reason, 'forbidden')
                self.assertIsNotNone((await self.adapter.fetch_message(ref)).content)


if __name__ == '__main__':
    unittest.main()
