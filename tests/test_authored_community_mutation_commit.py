"""Writer-transaction regressions for authored community mutation state.

An authored activity cannot advance only PublishedActivityObject while leaving
its Local/Remote Community canonical target at a stale physical revision.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from src.db import Database
from src.models import (
    CommunityMessageGroup, CommunityThreadGroup,
    LocalCommunity, LocalCommunityMessage, LocalCommunityThread,
    PublishedActivityObject, PlatformConnector, ChannelSurface,
    ChannelCommunitySubscription, UserActorOutboundSource, UserActorOutboundDelivery,
)


class AuthoredCommunityMutationCommitTests(unittest.TestCase):
    """Commit both canonical owners atomically or refuse invalid ownership."""

    def setUp(self) -> None:
        """Prepare an isolated current-v1 database and a registered author."""
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.database = Database(f"sqlite:///{Path(self.tempdir.name) / 'bridge.sqlite'}")
        self.addCleanup(self.database.close)
        self.database.migrate()
        self.actor = 'https://bridge.test/actors/users/alice'
        self.remote = 'https://remote.test/c/group'
        self.local = 'https://bridge.test/actors/communities/local'
        self.database.users.create_user(
            discord_user_id='123', activitypub_username='alice', actor_url=self.actor,
            inbox_url=f'{self.actor}/inbox', outbox_url=f'{self.actor}/outbox',
            followers_url=f'{self.actor}/followers', public_key_pem='public',
            private_key_pem='private',
        )

    def _prepare(self, domain: str, kind: str, *, wrong_owner: bool = False) -> str:
        """Create one published object and an exact or wrong-community domain row."""
        community = self.local if domain == 'local' else self.remote
        obj = f'https://bridge.test/objects/{domain}/{kind}'
        with self.database.immediate_session() as session:
            if domain == 'local':
                local = session.scalar(select(LocalCommunity).where(
                    LocalCommunity.actor_url == self.local
                ))
                if local is None:
                    local = LocalCommunity(
                        slug='local', display_name='Local', actor_url=self.local,
                        inbox_url=f'{self.local}/inbox', outbox_url=f'{self.local}/outbox',
                        followers_url=f'{self.local}/followers', public_key_pem='public',
                        private_key_pem='private', status='active',
                    )
                    session.add(local)
                    session.flush()
                thread = LocalCommunityThread(
                    local_community_id=local.id, ap_activity_id=f'{obj}/thread/create',
                    ap_object_id=obj if kind == 'post' else f'{obj}/parent',
                    direction='discord_to_ap', origin_kind='discord_local',
                    current_content_json=None, content_revision=0,
                )
                session.add(thread)
                session.flush()
                if kind == 'comment':
                    session.add(LocalCommunityMessage(
                        local_community_thread_id=thread.id,
                        ap_activity_id=f'{obj}/create', ap_object_id=obj,
                        direction='discord_to_ap', content_revision=0,
                    ))
            else:
                thread = CommunityThreadGroup(
                    community_actor_id=community if not wrong_owner else 'https://other.test/group',
                    ap_activity_id=f'{obj}/thread/create',
                    ap_object_id=obj if kind == 'post' else f'{obj}/parent',
                    content_revision=0,
                )
                session.add(thread)
                session.flush()
                if kind == 'comment':
                    session.add(CommunityMessageGroup(
                        thread_group_id=thread.id,
                        community_actor_id=thread.community_actor_id,
                        ap_activity_id=f'{obj}/create', ap_object_id=obj,
                        content_revision=0,
                    ))
            session.add(PublishedActivityObject(
                actor_username='alice', actor_url=self.actor,
                publication_context='community', community_actor_url=community,
                activity_id=f'{obj}/create', object_id=obj, kind=kind,
                title='Old' if kind == 'post' else None,
                body_markdown='old',
                conversation_root_object_id=obj if kind == 'post' else f'{obj}/parent',
                in_reply_to_object_id=None if kind == 'post' else f'{obj}/parent',
                content_revision=1,
            ))
        return obj

    def _commit(self, obj: str, *, delete: bool = False):
        """Use the real durable reservation and canonical-ready writer boundary."""
        repo = self.database.content_mutations
        if delete:
            self.assertEqual(repo.record_desired_delete(object_id=obj), 'accepted')
        else:
            self.assertEqual(repo.record_desired_update(
                object_id=obj, title='New' if '/post' in obj else None,
                body_markdown='new body',
                edited_timestamp=datetime(2026, 10, 22, tzinfo=timezone.utc),
            ), 'accepted')
        repo.reserve_next(object_id=obj)
        claim = repo.claim_due(limit=1, stale_after_seconds=60)[0]
        activity = {
            'id': claim.activity_id,
            'type': 'Delete' if delete else 'Update',
            'actor': self.actor,
            'object': obj,
        }
        self.assertTrue(repo.record_canonical_result(claim.mutation_id, claim.claim_token, activity))
        return repo.commit_canonical(
            mutation_id=claim.mutation_id, claim_token=claim.claim_token,
            canonical_activity_json=activity,
            delivery_repository=self.database.user_actor_deliveries,
        )

    def test_authored_updates_advance_both_canonical_domains_and_comments(self) -> None:
        """Every published community mutation updates its own canonical body/revision."""
        for domain in ('local', 'remote'):
            for kind in ('post', 'comment'):
                with self.subTest(domain=domain, kind=kind):
                    obj = self._prepare(domain, kind)
                    self.assertIsNotNone(self._commit(obj))
                    model = (LocalCommunityThread if kind == 'post' else LocalCommunityMessage) if domain == 'local' else (CommunityThreadGroup if kind == 'post' else CommunityMessageGroup)
                    with self.database.session() as session:
                        row = session.scalar(select(model).where(model.ap_object_id == obj))
                        published = session.scalar(select(PublishedActivityObject).where(PublishedActivityObject.object_id == obj))
                        self.assertEqual(published.content_revision, 2)
                        self.assertEqual(row.content_revision, 1)
                        snapshot = json.loads(row.current_content_json)
                        self.assertEqual(snapshot['bodyMarkdown'], 'new body')
                        self.assertEqual(snapshot['apObjectId'], obj)

    def test_authored_delete_tombstones_both_owners_atomically(self) -> None:
        """Terminal published Delete must not leave a live canonical community row."""
        for domain in ('local', 'remote'):
            with self.subTest(domain=domain):
                obj = self._prepare(domain, 'post')
                self.assertIsNotNone(self._commit(obj, delete=True))
                model = LocalCommunityThread if domain == 'local' else CommunityThreadGroup
                with self.database.session() as session:
                    row = session.scalar(select(model).where(model.ap_object_id == obj))
                    published = session.scalar(select(PublishedActivityObject).where(PublishedActivityObject.object_id == obj))
                    self.assertIsNotNone(published.deleted_at)
                    self.assertIsNotNone(row.deleted_at)
                    self.assertEqual(row.content_revision, 1)

    def _bind_verified_remote_forum(self, obj: str) -> int:
        """Bind the published source to a current accepted Group Follow."""
        with self.database.immediate_session() as session:
            connector = session.scalar(select(PlatformConnector).where(
                PlatformConnector.connector_key == 'discord-primary'))
            if connector is None:
                connector = PlatformConnector(connector_key='discord-primary', platform='discord')
                session.add(connector)
                session.flush()
            sub = ChannelCommunitySubscription(
                lemmy_community_actor_id=self.remote,
                community_inbox_url=f'{self.remote}/inbox',
                follow_activity_id='https://remote.test/follows/F1',
                status='accepted',
            )
            session.add(sub)
            session.flush()
            surface = ChannelSurface(
                connector_id=connector.id, external_channel_id='777',
                external_guild_id='1', kind='forum', remote_subscription_id=sub.id,
            )
            session.add(surface)
            session.flush()
            published = session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.object_id == obj))
            published.source_connector_id = connector.id
            published.source_channel_surface_id = surface.id
            return sub.id

    def test_author_update_admits_one_verified_direct_only_recipient(self) -> None:
        """A remote Group does not have to follow the author's User Actor."""
        obj = self._prepare('remote', 'post')
        self._bind_verified_remote_forum(obj)
        self.assertIsNotNone(self._commit(obj))
        with self.database.session() as session:
            source = session.scalar(select(UserActorOutboundSource).where(
                UserActorOutboundSource.source_kind == 'content_update'))
            delivery = session.scalar(select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == source.id))
            self.assertIsNotNone(delivery)
            evidence = json.loads(delivery.direct_authorizations_json)
            self.assertEqual(evidence[0]['recipientActorId'], self.remote)
            self.assertEqual(evidence[0]['followActivityId'], 'https://remote.test/follows/F1')
        claims = self.database.user_actor_deliveries.claim_due(
            limit=5, per_user_limit=5, stale_after_seconds=300
        )
        self.assertEqual(len(claims), 1)

    def test_direct_delete_requires_committed_canonical_tombstone(self) -> None:
        """A forged Delete source must never bypass the persisted terminal revision."""
        obj = self._prepare('remote', 'post')
        self._bind_verified_remote_forum(obj)
        self.assertIsNotNone(self._commit(obj))
        with self.database.immediate_session() as session:
            source = session.scalar(select(UserActorOutboundSource).where(
                UserActorOutboundSource.source_kind == 'content_update'))
            self.assertIsNotNone(source)
            # Simulate an inconsistent persisted delivery source. Authorization
            # must use the canonical row, not trust a self-described kind.
            source.source_kind = 'content_delete'
        self.assertEqual(self.database.user_actor_deliveries.claim_due(
            limit=5, per_user_limit=5, stale_after_seconds=300), [])

    def test_direct_delete_after_canonical_tombstone_is_claimable(self) -> None:
        """A valid Delete remains deliverable after its source becomes deleted."""
        obj = self._prepare('remote', 'post')
        self._bind_verified_remote_forum(obj)
        self.assertIsNotNone(self._commit(obj, delete=True))
        claims = self.database.user_actor_deliveries.claim_due(
            limit=5, per_user_limit=5, stale_after_seconds=300)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].activity_json['type'], 'Delete')

    def test_author_mutation_after_follow_rebind_does_not_reuse_old_inbox(self) -> None:
        """A new or unaccepted Group is never inferred from stale Create recipients."""
        obj = self._prepare('remote', 'post')
        subscription_id = self._bind_verified_remote_forum(obj)
        with self.database.immediate_session() as session:
            sub = session.get(ChannelCommunitySubscription, subscription_id)
            sub.status = 'removed'
        self.assertIsNotNone(self._commit(obj))
        with self.database.session() as session:
            source = session.scalar(select(UserActorOutboundSource).where(
                UserActorOutboundSource.source_kind == 'content_update'))
            deliveries = list(session.scalars(select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == source.id)))
            self.assertEqual(deliveries, [])

    def test_render_only_gateway_runs_before_commit_and_cannot_change_identity(self) -> None:
        """An authored mutation only becomes sendable after a verified render commit."""
        import asyncio
        from types import SimpleNamespace
        from src.user_actor_delivery import UserActorDeliveryService
        from src.fedify_gateway_client import CanonicalMutationResult
        obj = self._prepare('remote', 'post')
        self._bind_verified_remote_forum(obj)
        repo = self.database.content_mutations
        self.assertEqual(repo.record_desired_update(
            object_id=obj, title='New', body_markdown='new body',
            edited_timestamp=datetime(2026, 10, 22, tzinfo=timezone.utc),
        ), 'accepted')
        repo.reserve_next(object_id=obj)
        calls = []
        class RenderOnlyGateway:
            """Observe SQLite just before the pure gateway constructor returns."""
            async def update_content(inner, request):
                calls.append(request.activity_id)
                with self.database.session() as session:
                    published = session.scalar(select(PublishedActivityObject).where(
                        PublishedActivityObject.object_id == obj))
                    canonical = session.scalar(select(CommunityThreadGroup).where(
                        CommunityThreadGroup.ap_object_id == obj))
                    self.assertEqual((published.content_revision, canonical.content_revision), (1, 0))
                    self.assertEqual(list(session.scalars(select(UserActorOutboundSource))), [])
                return CanonicalMutationResult(request.activity_id, obj, {
                    'id': request.activity_id, 'type': 'Update',
                    'actor': self.actor, 'object': {'id': obj},
                })
        service = UserActorDeliveryService(
            self.database, RenderOnlyGateway(),
            SimpleNamespace(federation_decision=lambda _target: SimpleNamespace(allowed=True)),
        )
        self.assertEqual(asyncio.run(service.drain_mutations(limit=1)), 1)
        self.assertEqual(len(calls), 1)
        with self.database.session() as session:
            published = session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.object_id == obj))
            canonical = session.scalar(select(CommunityThreadGroup).where(
                CommunityThreadGroup.ap_object_id == obj))
            delivery = session.scalar(select(UserActorOutboundDelivery))
            self.assertEqual((published.content_revision, canonical.content_revision), (2, 1))
            self.assertIsNotNone(delivery)

    def test_mismatched_render_result_never_commits_canonical_revision(self) -> None:
        """A gateway-supplied activity ID cannot replace the claimed author's ID."""
        import asyncio
        from types import SimpleNamespace
        from src.user_actor_delivery import UserActorDeliveryService
        from src.fedify_gateway_client import CanonicalMutationResult
        obj = self._prepare('remote', 'post')
        self._bind_verified_remote_forum(obj)
        repo = self.database.content_mutations
        self.assertEqual(repo.record_desired_update(
            object_id=obj, title='New', body_markdown='new body',
            edited_timestamp=datetime(2026, 10, 22, tzinfo=timezone.utc),
        ), 'accepted')
        repo.reserve_next(object_id=obj)
        class ForgedGateway:
            """Return a plausible but unrelated rendered ActivityPub identity."""
            async def update_content(inner, request):
                return CanonicalMutationResult(request.activity_id, obj, {
                    'id': 'https://foreign.test/forged', 'type': 'Update',
                    'actor': self.actor, 'object': obj,
                })
        service = UserActorDeliveryService(
            self.database, ForgedGateway(),
            SimpleNamespace(federation_decision=lambda _target: SimpleNamespace(allowed=True)),
        )
        asyncio.run(service.drain_mutations(limit=1))
        with self.database.session() as session:
            published = session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.object_id == obj))
            canonical = session.scalar(select(CommunityThreadGroup).where(
                CommunityThreadGroup.ap_object_id == obj))
            self.assertEqual((published.content_revision, canonical.content_revision), (1, 0))
            self.assertEqual(list(session.scalars(select(UserActorOutboundSource))), [])

    def test_mismatched_remote_canonical_owner_rolls_back_publication(self) -> None:
        """An unrelated remote Group cannot be mutated by another Group's publication."""
        obj = self._prepare('remote', 'post', wrong_owner=True)
        with self.assertRaises(ValueError):
            self._commit(obj, delete=True)
        with self.database.session() as session:
            published = session.scalar(select(PublishedActivityObject).where(PublishedActivityObject.object_id == obj))
            row = session.scalar(select(CommunityThreadGroup).where(CommunityThreadGroup.ap_object_id == obj))
            self.assertIsNone(published.deleted_at)
            self.assertIsNone(row.deleted_at)
            self.assertEqual(published.content_revision, 1)
            self.assertEqual(row.content_revision, 0)


if __name__ == '__main__':
    unittest.main()
