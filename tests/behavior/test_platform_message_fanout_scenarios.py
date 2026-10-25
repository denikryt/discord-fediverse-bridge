"""Checkpoint 63: real SQLite multi-forum fan-out with connector-neutral IO.

The original human post starts in subscriber B. A and C receive independent
physical thread/starter copies; a failed C never causes A to be reposted.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from src.content_sync.platform_fanout import PlatformFanoutCoordinator
from src.db import Database
from src.models import (
    ChannelSurface, LocalCommunity, LocalCommunityThread, LocalCommunityThreadSurface,
    LocalCommunityMessage, LocalCommunityMessageSurface, LocalSubscriber,
    PlatformConnector, PlatformDeliveryIntent,
)
from src.platform_types import (
    ChannelRef, ContentDocument, CreateAttempt, CreatedMessage, CreatedThread,
    DeliveryPresentation, MessageRef, ThreadRef,
)


class _ForumAdapter:
    """Record platform effects while returning independently scoped receipts."""

    def __init__(self):
        """Initialize a deterministic test transport without network IO."""
        self.posts: list[str] = []
        self.comments: list[tuple[str, str | None]] = []
        self.fail_forums: set[str] = set()

    async def create_forum_thread(self, forum, content, *, title, presentation,
                                  attempt: CreateAttempt):
        """Create one fake thread per forum or lose the response after POST."""
        self.posts.append(forum.external_channel_id)
        if forum.external_channel_id in self.fail_forums:
            raise ConnectionError('Discord accepted POST but response was lost')
        thread = f'created-{forum.external_channel_id}'
        return CreatedThread(ThreadRef(forum, thread),
                             MessageRef(forum.connector_key, thread, f'starter-{thread}'),
                             'bot')

    async def send_message(self, thread, content, *, reply_to, presentation,
                           attempt: CreateAttempt):
        """Send a fake reply and retain its target-local physical parent identity."""
        self.comments.append((thread.external_thread_id,
                              reply_to.external_message_id if reply_to else None))
        return CreatedMessage(MessageRef(thread.channel.connector_key,
                                         thread.external_thread_id,
                                         f'child-{thread.external_thread_id}'), 'bot')


class _Registry:
    """Select the connector without any SDK or hardcoded numeric IDs."""

    def __init__(self, adapter):
        """Use a single test adapter for the registered connector."""
        self.adapter = adapter

    def get(self, connector_key):
        """Reject falling back to another connector's physical transport."""
        if connector_key != 'discord-primary':
            raise LookupError(connector_key)
        return self.adapter


class _ThreeForumFixture(unittest.TestCase):
    """B as author fans out to A and C with independent durable receipts."""

    def setUp(self):
        """Persist one Local Community, host A, subscribers B and C, and a post."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Database(f'sqlite:///{Path(temporary.name) / "bridge.db"}')
        self.addCleanup(self.database.close)
        self.database.migrate()
        self.forums: dict[str, ChannelRef] = {}
        self.targets: dict[str, tuple[str, int, int]] = {}
        with self.database.immediate_session() as session:
            community = LocalCommunity(
                slug='multi', display_name='Multi',
                actor_url='https://bridge.test/communities/multi',
                inbox_url='https://bridge.test/communities/multi/inbox',
                outbox_url='https://bridge.test/communities/multi/outbox',
                followers_url='https://bridge.test/communities/multi/followers',
                public_key_pem='test-public', private_key_pem='test-private',
                status='active',
            )
            session.add(community)
            session.flush()
            self.community_id = community.id
            connector = session.scalar(select(PlatformConnector).where(
                PlatformConnector.connector_key == 'discord-primary'))
            for label, external in [('A', '100'), ('B', '200'), ('C', '300')]:
                if label == 'A':
                    owner_kind, owner_id = 'local_host', community.id
                    surface = ChannelSurface(
                        connector_id=connector.id, external_channel_id=external,
                        external_guild_id='1', kind='forum',
                        local_community_id=community.id,
                    )
                else:
                    subscriber = LocalSubscriber(
                        local_community_id=community.id,
                        discord_channel_id=int(external), discord_guild_id=1,
                        status='active',
                    )
                    session.add(subscriber)
                    session.flush()
                    owner_kind, owner_id = 'local_subscriber', subscriber.id
                    surface = ChannelSurface(
                        connector_id=connector.id, external_channel_id=external,
                        external_guild_id='1', kind='forum',
                        local_subscriber_id=subscriber.id,
                    )
                session.add(surface)
                session.flush()
                self.forums[label] = ChannelRef('discord-primary', external,
                                                'forum', '1')
                self.targets[label] = (owner_kind, owner_id, surface.id)
            source = LocalCommunityThread(
                local_community_id=community.id,
                ap_activity_id='https://bridge.test/activities/source',
                ap_object_id='https://bridge.test/objects/source',
                direction='outbound', origin_kind='discord',
            )
            session.add(source)
            session.flush()
            self.canonical_id = source.id
        self.adapter = _ForumAdapter()
        self.coordinator = PlatformFanoutCoordinator(self.database,
                                                     _Registry(self.adapter))

    async def _deliver(self, forum_label: str) -> bool:
        """Route an independently frozen target for one authored source."""
        owner_kind, owner_id, surface_id = self.targets[forum_label]
        forum = self.forums[forum_label]
        return await self.coordinator.stage_and_deliver(
            domain='local', kind='post', canonical_id=self.canonical_id,
            forum=forum, operation='forum_thread', destination=forum,
            content=ContentDocument('human post from B'),
            presentation=DeliveryPresentation('bot'), title='B source',
        )

class SharedBoundaryScenarios(unittest.TestCase):
    """Guard platform-neutral source and coordinator code against SDK leakage."""

    def test_shared_modules_have_no_discord_projection_or_sdk_import(self):
        """Author identity may be Discord-specific only inside its owning repository."""
        source = Path(__file__).resolve().parents[2] / 'src'
        files = [
            source / 'content_sync' / name for name in (
                'surface_resolution.py', 'parent_mapping.py',
                'platform_fanout.py', 'platform_mutations.py',
                'delivery_recovery.py',
            )
        ] + [source / 'db' / 'repositories' / 'platform_delivery_intents.py']
        for file in files:
            with self.subTest(module=file.name):
                content = file.read_text()
                self.assertNotIn('discord_id_to_legacy_int', content)
                self.assertNotIn('discord_', content.lower())
                self.assertNotIn('import discord', content.lower())
                self.assertNotIn('int(external_', content)

    def test_delayed_comment_presentation_matches_current_discord_output(self):
        """Rehydrating an AP comment must preserve attribution and length limits."""
        from src.formatting import (
            format_federated_comment_message, format_lemmy_comment_for_discord,
        )
        source = ('Remote', 'comment body', 'https://remote.test/post',
                  'https://remote.test/u/member')
        self.assertEqual(format_federated_comment_message(*source),
                         format_lemmy_comment_for_discord(*source))
        self.assertEqual(format_federated_comment_message(*source),
                         '`Remote@remote.test`\n\ncomment body')
        self.assertLessEqual(len(format_federated_comment_message(
            source[0], 'x' * 3000, source[2], actor_id=source[3])), 2000)
        with self.assertRaises(ValueError):
            format_federated_comment_message(*source, max_chars=0)


class LocalCommunityThreeForumScenarios(_ThreeForumFixture):
    """Multi-recipient observable fanout and target isolation scenarios."""

    def test_b_source_reaches_a_c_once_with_distinct_physical_ids(self):
        """Human post in B creates two bot copies, no third echo or duplicate."""
        self.assertTrue(asyncio.run(self._deliver('A')))
        self.assertTrue(asyncio.run(self._deliver('C')))
        self.assertFalse(asyncio.run(self._deliver('A')))
        self.assertEqual(self.adapter.posts, ['100', '300'])
        with self.database.session() as session:
            receipts = session.scalars(select(LocalCommunityThreadSurface).where(
                LocalCommunityThreadSurface.local_community_thread_id == self.canonical_id,
            )).all()
            self.assertEqual({row.external_thread_id for row in receipts},
                             {'created-100', 'created-300'})
            self.assertEqual({row.external_starter_message_id for row in receipts},
                             {'starter-created-100', 'starter-created-300'})
            intents = session.scalars(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.domain == 'local',
                PlatformDeliveryIntent.canonical_row_id == self.canonical_id,
            )).all()
            self.assertEqual({row.state for row in intents}, {'delivered'})
            self.assertEqual(len(intents), 2)

    def test_c_lost_response_does_not_block_a_or_repost_after_restart(self):
        """One ambiguous physical target is sticky while the healthy target completes."""
        self.adapter.fail_forums.add('300')
        self.assertFalse(asyncio.run(self._deliver('C')))
        self.assertTrue(asyncio.run(self._deliver('A')))
        self.assertEqual(self.adapter.posts, ['300', '100'])
        with self.database.session() as session:
            states = dict((row.channel_surface_id, row.state) for row in
                          session.scalars(select(PlatformDeliveryIntent)))
            self.assertEqual(states[self.targets['C'][2]], 'unresolved')
            self.assertEqual(states[self.targets['A'][2]], 'delivered')
        # A new coordinator, same persisted DB, cannot replay uncertain C.
        restart = PlatformFanoutCoordinator(self.database, _Registry(self.adapter))
        self.assertEqual(asyncio.run(restart.recover_pending()), [])
        self.assertEqual(self.adapter.posts, ['300', '100'])

    def _sibling_parent_replies(self):
        """Create per-forum confirmed copies of one canonical nested parent."""
        self.assertTrue(asyncio.run(self._deliver('A')))
        self.assertTrue(asyncio.run(self._deliver('C')))
        with self.database.immediate_session() as session:
            parent = LocalCommunityMessage(
                local_community_thread_id=self.canonical_id,
                ap_activity_id='https://remote.test/activities/parent',
                ap_object_id='https://remote.test/objects/parent',
                direction='inbound',
            )
            session.add(parent)
            session.flush()
            child = LocalCommunityMessage(
                local_community_thread_id=self.canonical_id,
                ap_activity_id='https://remote.test/activities/child',
                ap_object_id='https://remote.test/objects/child',
                parent_ap_object_id=parent.ap_object_id,
                direction='inbound',
            )
            session.add(child)
            session.flush()
            child_id = child.id
            targets: dict[str, tuple[ThreadRef, MessageRef]] = {}
            for label in ('A', 'C'):
                _, _, surface_id = self.targets[label]
                root = session.scalar(select(LocalCommunityThreadSurface).where(
                    LocalCommunityThreadSurface.local_community_thread_id == self.canonical_id,
                    LocalCommunityThreadSurface.channel_surface_id == surface_id,
                ))
                parent_ref = MessageRef('discord-primary', root.external_thread_id,
                                        f'parent-{label}')
                session.add(LocalCommunityMessageSurface(
                    local_community_message_id=parent.id,
                    local_community_thread_surface_id=root.id,
                    connector_id=root.connector_id, channel_surface_id=surface_id,
                    external_message_id=parent_ref.external_message_id,
                    role='host' if label == 'A' else 'local_subscriber',
                ))
                targets[label] = (ThreadRef(self.forums[label], root.external_thread_id),
                                  parent_ref)
        return child_id, targets

    def test_nested_reply_uses_independent_confirmed_parent_per_forum(self):
        """One canonical comment fans out to each sibling's real parent copy."""
        child_id, targets = self._sibling_parent_replies()
        for label in ('A', 'C'):
            destination, parent_ref = targets[label]
            self.assertTrue(asyncio.run(self.coordinator.stage_and_deliver(
                domain='local', kind='comment', canonical_id=child_id,
                forum=self.forums[label], operation='message', destination=destination,
                content=ContentDocument('new nested reply'),
                presentation=DeliveryPresentation('bot'), reply_to=parent_ref,
            )))
        self.assertEqual(self.adapter.comments,
                         [('created-100', 'parent-A'), ('created-300', 'parent-C')])
        with self.database.session() as session:
            copies = session.scalars(select(LocalCommunityMessageSurface).where(
                LocalCommunityMessageSurface.local_community_message_id == child_id,
            )).all()
            self.assertEqual({(copy.external_message_id, copy.external_parent_message_id)
                              for copy in copies},
                             {('child-created-100', 'parent-A'),
                              ('child-created-300', 'parent-C')})

    def test_nested_reply_cannot_borrow_another_forums_parent_message(self):
        """A forged sibling parent cannot become a valid local reply reference."""
        child_id, targets = self._sibling_parent_replies()
        destination, _ = targets['C']
        # A physical id from A is never evidence for a parent copy in C.
        forged = MessageRef('discord-primary', destination.external_thread_id,
                            targets['A'][1].external_message_id)
        with self.assertRaises(ValueError):
            asyncio.run(self.coordinator.stage_and_deliver(
                domain='local', kind='comment', canonical_id=child_id,
                forum=self.forums['C'], operation='message', destination=destination,
                content=ContentDocument('wrong target parent'),
                presentation=DeliveryPresentation('bot'), reply_to=forged,
            ))
        self.assertEqual(self.adapter.comments, [])
        with self.database.session() as session:
            self.assertEqual(session.scalars(select(PlatformDeliveryIntent).where(
                PlatformDeliveryIntent.canonical_row_id == child_id,
                PlatformDeliveryIntent.object_kind == 'comment',
            )).all(), [])

    def test_unsubscribed_target_is_cancelled_before_transport(self):
        """A subscriber cancellation between admission and claim prevents new POST."""
        label = 'C'
        forum = self.forums[label]
        kind, owner_id, surface_id = self.targets[label]
        attempt = CreateAttempt(
            f'fanout:v1:local:post:{self.canonical_id}:discord-primary:{surface_id}',
            'discord-primary', 'forum_thread', forum,
        )
        row = self.database.platform_delivery_intents.reserve(
            domain='local', kind='post', canonical_id=self.canonical_id,
            forum=forum, channel_surface_id=surface_id,
            owner_kind=kind, owner_id=owner_id, attempt=attempt,
            content=ContentDocument('human post from B'),
            presentation=DeliveryPresentation('bot'), title='B source',
        )
        with self.database.immediate_session() as session:
            session.get(LocalSubscriber, owner_id).status = 'inactive'
        self.assertFalse(asyncio.run(self.coordinator.deliver(row.id)))
        self.assertEqual(self.adapter.posts, [])
        with self.database.session() as session:
            self.assertEqual(session.get(PlatformDeliveryIntent, row.id).state,
                             'cancelled')


if __name__ == '__main__':
    unittest.main()
