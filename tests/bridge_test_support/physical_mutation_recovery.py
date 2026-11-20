"""Durable physical mutation scenarios on real SQLite v1 delivery rows."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from src.db import Database
from src.models import (
    ChannelSurface, LocalCommunity, LocalCommunityThread,
    LocalCommunityThreadSurface, PlatformConnector,
)
from src.platform_types import AuthorRef, ContentDocument, MessageRef, MessageView


class PhysicalMutationScenario:
    """Seed durable SQLite copies and provide the shared SDK-boundary harness."""

    def setUp(self) -> None:
        """Use a destination message that already exists, without a Create intent."""
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.db = Database(f"sqlite:///{Path(t.name) / 'bridge.db'}")
        self.addCleanup(self.db.close)
        self.db.migrate()
        with self.db.immediate_session() as session:
            community = LocalCommunity(
                slug='local', display_name='Local',
                actor_url='https://bridge.test/actors/communities/local',
                inbox_url='https://bridge.test/actors/communities/local/inbox',
                outbox_url='https://bridge.test/actors/communities/local/outbox',
                followers_url='https://bridge.test/actors/communities/local/followers',
                public_key_pem='public', private_key_pem='private', status='active',
            )
            session.add(community)
            session.flush()
            connector = session.scalar(select(PlatformConnector).where(
                PlatformConnector.connector_key == 'discord-primary'))
            surface = ChannelSurface(
                connector_id=connector.id, external_channel_id='500',
                external_guild_id='1', kind='forum',
                local_community_id=community.id,
            )
            session.add(surface)
            session.flush()
            post = LocalCommunityThread(
                local_community_id=community.id,
                ap_activity_id='https://bridge.test/create/1',
                ap_object_id='https://remote.test/post/1',
                origin_kind='remote_follower', direction='ap_to_discord',
                content_revision=1,
                current_content_json=json.dumps({
                    'schemaVersion': 1, 'kind': 'post',
                    'apObjectId': 'https://remote.test/post/1',
                    'authorName': 'Alice',
                    'authorActorId': 'https://remote.test/users/alice',
                    'title': 'Changed', 'bodyMarkdown': 'latest',
                }),
            )
            session.add(post)
            session.flush()
            copy = LocalCommunityThreadSurface(
                local_community_thread_id=post.id,
                connector_id=connector.id, channel_surface_id=surface.id,
                external_thread_id='900', external_starter_message_id='901',
                discord_forum_channel_id=500, discord_thread_id=900,
                discord_starter_message_id=901,
                role='host', applied_revision=None,
            )
            session.add(copy)
            session.flush()
            self.copy_id = copy.id
            self.post_id = post.id
            self.surface_id = surface.id


    def _worker(self, *, fail: bool = False):
        """Use a typed registry with a deterministic SDK-boundary fake."""
        from src.content_sync.platform_mutations import PlatformPhysicalMutationCoordinator
        calls = []
        class Adapter:
            """Expose exact typed message edit/delete boundary."""
            async def fetch_message(self, message):
                """Resolve this scenario's exact bot-owned physical copy."""
                return MessageView(message, AuthorRef(message.connector_key, 'bot', 'bot', 'Bridge', True))
            async def edit_message(self, message, content):
                """Record one physical edit, possibly losing the ACK."""
                calls.append(('edit', message, content.body))
                if fail:
                    raise ConnectionError('lost edit response')
            async def delete_message(self, message):
                """Record the exact physical delete."""
                calls.append(('delete', message, None))
                if fail:
                    raise ConnectionError('lost delete response')
        class Registry:
            """Refuse fallback adapters from other connectors."""
            def get(self, key):
                """Return only the explicitly registered fake connector."""
                if key != 'discord-primary':
                    raise AssertionError('wrong connector')
                return Adapter()
        worker = PlatformPhysicalMutationCoordinator(
            self.db, Registry(),
            target_policy=lambda connector_key, surface_id: connector_key == 'discord-primary' and surface_id == self.surface_id,
            content_renderer=lambda _domain, _kind, snapshot: ContentDocument(snapshot['bodyMarkdown']),
        )
        return worker, calls


    def _add_older_settled_copies_and_new_lagging_copy(self) -> int:
        """Place a legitimate lagging copy behind eight already-settled rows.

        The worker's batch limit applies to actual claimable operations, not
        the first physical-copy rows that happen to be indexed by SQLite.
        """
        with self.db.immediate_session() as session:
            initial_copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            initial_copy.applied_revision = 1
            connector_id = initial_copy.connector_id
            community_id = session.get(LocalCommunityThread, self.post_id).local_community_id
            for index in range(7):
                post = LocalCommunityThread(
                    local_community_id=community_id,
                    ap_activity_id=f'https://bridge.test/create/settled-{index}',
                    ap_object_id=f'https://bridge.test/post/settled-{index}',
                    origin_kind='remote_follower', direction='ap_to_discord',
                    content_revision=1,
                )
                session.add(post)
                session.flush()
                session.add(LocalCommunityThreadSurface(
                    local_community_thread_id=post.id,
                    connector_id=connector_id, channel_surface_id=self.surface_id,
                    external_thread_id=f'settled-{index}',
                    external_starter_message_id=f'settled-msg-{index}',
                    role='host', applied_revision=1,
                ))
            lagging = LocalCommunityThread(
                local_community_id=community_id,
                ap_activity_id='https://bridge.test/create/lagging',
                ap_object_id='https://bridge.test/post/lagging',
                origin_kind='remote_follower', direction='ap_to_discord',
                content_revision=1,
                current_content_json=json.dumps({
                    'schemaVersion': 1, 'kind': 'post',
                    'apObjectId': 'https://bridge.test/post/lagging',
                    'bodyMarkdown': 'please deliver this edit',
                }),
            )
            session.add(lagging)
            session.flush()
            last_copy = LocalCommunityThreadSurface(
                local_community_thread_id=lagging.id,
                connector_id=connector_id, channel_surface_id=self.surface_id,
                external_thread_id='lagging-thread',
                external_starter_message_id='lagging-message',
                role='host', applied_revision=None,
            )
            session.add(last_copy)
            session.flush()
            return last_copy.id


    def _add_healthy_recovery_neighbor(self) -> int:
        """Create an independent due copy beside an unsupported source snapshot."""
        with self.db.immediate_session() as session:
            original = session.get(LocalCommunityThread, self.post_id)
            original_copy = session.get(LocalCommunityThreadSurface, self.copy_id)
            post = LocalCommunityThread(
                local_community_id=original.local_community_id,
                ap_activity_id='https://bridge.test/create/healthy-neighbor',
                ap_object_id='https://bridge.test/post/healthy-neighbor',
                origin_kind='remote_follower', direction='ap_to_discord',
                content_revision=1,
                current_content_json=json.dumps({
                    'schemaVersion': 1, 'kind': 'post',
                    'apObjectId': 'https://bridge.test/post/healthy-neighbor',
                    'bodyMarkdown': 'healthy after failure',
                }),
            )
            session.add(post)
            session.flush()
            neighbor = LocalCommunityThreadSurface(
                local_community_thread_id=post.id,
                connector_id=original_copy.connector_id,
                channel_surface_id=self.surface_id,
                external_thread_id='healthy-thread',
                external_starter_message_id='healthy-msg',
                role='host', applied_revision=None,
            )
            session.add(neighbor)
            session.flush()
            return neighbor.id
