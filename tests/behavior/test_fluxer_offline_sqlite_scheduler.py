"""One SQLite runtime scheduler delivers Discord while Fluxer is offline."""

from __future__ import annotations

import asyncio
import unittest

from sqlalchemy import select

from src.content_sync.platform_delivery_scheduler import ConnectorDispatchCapacity, PlatformPhysicalDeliveryScheduler
from src.content_sync.platform_fanout import PlatformFanoutCoordinator
from src.db.repositories.platform_surfaces import PlatformSurfaceRepository
from src.models import ChannelSurface, PlatformConnector, PlatformDeliveryIntent
from src.platform_types import ChannelRef, ContentDocument, CreateAttempt, DeliveryPresentation

from .test_platform_message_fanout_scenarios import _ForumAdapter, _ThreeForumFixture


class OfflineFluxerSqliteSchedulerScenarios(_ThreeForumFixture):
    """Use an explicitly provisioned test destination, never auto-bind communities."""

    def test_offline_fluxer_remains_pristine_while_discord_commits(self) -> None:
        """B13: pending → no claim while offline → one correct receipt after READY."""
        with self.database.immediate_session() as session:
            connector = PlatformSurfaceRepository.add_connector(
                session, connector_key='fluxer-one', platform='fluxer',
                instance_origin='https://trusted.test')
            session.flush()
            owner_kind, owner_id, original_id = self.targets['C']
            old = session.get(ChannelSurface, original_id)
            # Move the *explicit test-only* subscriber claim to a physical
            # Fluxer forum; this is not a production community provisioning API.
            old.local_subscriber_id = None
            session.flush()
            surface = ChannelSurface(
                connector_id=connector.id, external_channel_id='forum-fluxer',
                external_guild_id='guild-1', kind='forum', local_subscriber_id=owner_id)
            session.add(surface)
            session.flush()
            self.targets['C'] = (owner_kind, owner_id, surface.id)
            self.forums['C'] = ChannelRef('fluxer-one', 'forum-fluxer', 'forum', 'guild-1')

        class VerifiedFake(_ForumAdapter):
            """Record physical state only after this adapter is independently ready."""
            def __init__(inner, ready: bool):
                """Start one sidecar offline without granting an implicit send."""
                super().__init__()
                inner.ready = ready
                inner.received = asyncio.Event()
            def dispatch_ready(inner) -> bool:
                """Enforce an explicit readiness observation before a claim."""
                return inner.ready
            async def refresh_readiness(inner) -> bool:
                """An offline SDK yields without touching another lane."""
                return inner.ready
            async def create_forum_thread(inner, *args, **kwargs):
                """Expose a remote side effect and exact typed receipt."""
                assert inner.ready
                receipt = await super().create_forum_thread(*args, **kwargs)
                inner.received.set()
                return receipt

        discord = VerifiedFake(True)
        fluxer = VerifiedFake(False)
        class Registry:
            """Admit exactly the two independent runtime connectors."""
            def get(inner, key):
                """No fallback or second Fluxer instance is possible."""
                if key == 'discord-primary':
                    return discord
                if key == 'fluxer-one':
                    return fluxer
                raise LookupError(key)
        registry = Registry()
        gate = ConnectorDispatchCapacity(('discord-primary', 'fluxer-one'))
        coordinator = PlatformFanoutCoordinator(self.database, registry, dispatch_gate=gate)
        class NoMutations:
            """There are no revision-bearing physical copies in this scenario."""
            async def drain(inner, *, connector_key, limit):
                """Do not add a separate send owner to the Create test."""
                assert connector_key in gate.connector_keys and limit == 1
                return 0
        scheduler = PlatformPhysicalDeliveryScheduler(
            connector_keys=gate.connector_keys, fanout=coordinator,
            mutations=NoMutations(), poll_seconds=30)
        gate.bind_wake(scheduler.wake)
        coordinator.bind_delivery_wake(scheduler.wake)

        intent_ids: dict[str, int] = {}
        for label in ('A', 'C'):
            forum = self.forums[label]
            owner_kind, owner_id, surface_id = self.targets[label]
            attempt = CreateAttempt(
                f'fanout:v1:local:post:{self.canonical_id}:{forum.connector_key}:{surface_id}',
                forum.connector_key, 'forum_thread', forum)
            intent = self.database.platform_delivery_intents.reserve(
                domain='local', kind='post', canonical_id=self.canonical_id,
                forum=forum, channel_surface_id=surface_id, owner_kind=owner_kind,
                owner_id=owner_id, attempt=attempt, content=ContentDocument('remote text'),
                presentation=DeliveryPresentation('bot'), title='Remote post')
            intent_ids[label] = intent.id

        async def run() -> None:
            """Observe durable intermediate states before and after Node READY."""
            scheduler.start()
            try:
                await asyncio.wait_for(discord.received.wait(), 5)
                with self.database.session() as session:
                    a = session.get(PlatformDeliveryIntent, intent_ids['A'])
                    c = session.get(PlatformDeliveryIntent, intent_ids['C'])
                    self.assertEqual(a.state, 'delivered')
                    self.assertEqual(c.state, 'pending')
                    self.assertIsNone(c.dispatch_token)
                    self.assertIsNone(c.last_error_kind)
                self.assertEqual(fluxer.posts, [])
                fluxer.ready = True
                scheduler.wake('fluxer-one')
                await asyncio.wait_for(fluxer.received.wait(), 5)
                with self.database.session() as session:
                    c = session.get(PlatformDeliveryIntent, intent_ids['C'])
                    self.assertEqual(c.state, 'delivered')
                scheduler.wake('fluxer-one')
                self.assertEqual(discord.posts, ['100'])
                self.assertEqual(fluxer.posts, ['forum-fluxer'])
            finally:
                await scheduler.stop()

        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
