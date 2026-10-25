"""Typed source ingress must not mistake another physical container for the author."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

# Import the real dependency without constructing a live Discord client.
import discord  # Load the installed SDK; never leave a global placeholder for other suites.

from sqlalchemy import select

from src.db import Database
from src.discord_message_mutations import DiscordMessageMutationHandler
from src.models import (
    ChannelSurface, LocalCommunity, LocalCommunityThread, LocalCommunityMessage,
    ChannelCommunitySubscription, CommunityThreadGroup, MessageMapping,
    PlatformConnector, PublishedActivityObject, User,
)
from src.platform_types import AuthorRef, ConnectorRef, ContentDocument, MessageDeleted, MessageRef, MessageUpdated, MessageView, PlatformOperationError


REF = MessageRef('discord-primary', '900', '901')
WHEN = datetime(2026, 10, 23, 9, tzinfo=timezone.utc)


class _Reader:
    """An SDK-free boundary with verified connector metadata."""

    connector = ConnectorRef('discord-primary', 'discord')

    def __init__(self, *, view=None, failure=None):
        """Configure a verified read or an unavailable external adapter."""
        self.view, self.failure = view, failure

    async def fetch_message(self, ref):
        """Read exact typed identity or report the configured SDK error."""
        if self.failure is not None:
            raise self.failure
        if self.view is None:
            raise AssertionError('unexpected message fetch')
        return self.view


class TypedCommunityMutationIngressScenarios(unittest.TestCase):
    """Authenticated source ref and forum owner are necessary for author mutations."""

    def setUp(self):
        """Build one source and one unrelated physical message ID collision."""
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db=Database(f'sqlite:///{Path(temp.name)/"db.sqlite"}')
        self.addCleanup(self.db.close)
        self.db.migrate()
        with self.db.immediate_session() as session:
            user=User(discord_user_id='55', activitypub_username='alice',
                actor_url='https://bridge.test/users/alice',
                inbox_url='https://bridge.test/users/alice/inbox',
                outbox_url='https://bridge.test/users/alice/outbox',
                followers_url='https://bridge.test/users/alice/followers',
                public_key_pem='public', private_key_pem='private')
            community=LocalCommunity(slug='town',display_name='Town',
                actor_url='https://bridge.test/communities/town',
                inbox_url='https://bridge.test/communities/town/inbox',
                outbox_url='https://bridge.test/communities/town/outbox',
                followers_url='https://bridge.test/communities/town/followers',
                public_key_pem='public',private_key_pem='private',status='active')
            session.add_all((user,community));session.flush()
            self.community_id=community.id
            connector=session.scalar(select(PlatformConnector).where(PlatformConnector.connector_key=='discord-primary'))
            self.connector_id=connector.id
            surface=ChannelSurface(connector_id=connector.id,external_channel_id='100',external_guild_id='42',kind='forum',local_community_id=community.id)
            session.add(surface);session.flush()
            self.surface_id=surface.id
            source=LocalCommunityThread(local_community_id=community.id,
                ap_activity_id='https://bridge.test/create/1',ap_object_id='https://bridge.test/objects/1',
                direction='discord_to_ap',origin_kind='discord_human',
                source_connector_id=connector.id,source_channel_surface_id=surface.id,
                source_external_thread_id=REF.external_container_id,
                source_external_starter_message_id=REF.external_message_id)
            pub=PublishedActivityObject(actor_username='alice',actor_url=user.actor_url,
                activity_id=source.ap_activity_id,object_id=source.ap_object_id,
                publication_context='community',community_actor_url=community.actor_url,
                kind='post',title='hello',body_markdown='original',
                source_connector_id=connector.id,source_channel_surface_id=surface.id,
                source_external_container_id=REF.external_container_id,
                source_external_message_id=REF.external_message_id)
            mapping=MessageMapping(source_platform='discord',source_id=REF.external_message_id,
                activity_id=source.ap_activity_id,object_id=source.ap_object_id,
                actor_url=user.actor_url,community_actor_url=community.actor_url,
                source_connector_id=connector.id,source_channel_surface_id=surface.id,
                source_external_container_id=REF.external_container_id,
                discord_channel_id=100,discord_message_id=901)
            session.add_all((source,pub,mapping));session.flush()
        self.service=SimpleNamespace(submit_update=AsyncMock(return_value='accepted'), submit_delete=AsyncMock(return_value='accepted'))
        self.wake=Mock()
        self.router=SimpleNamespace(handle_message_edit=AsyncMock(), handle_message_delete=AsyncMock(),
            _is_guild_allowed=lambda guild: guild==42)
        self.handler=DiscordMessageMutationHandler(database=self.db,event_router=self.router,
            discord_fetcher=SimpleNamespace(),mutation_context_provider=lambda: SimpleNamespace(
                user_actor_delivery_service=self.service,user_actor_delivery_wake=SimpleNamespace(wake=self.wake)))

    def _edit(self, ref=REF, *, actor='55', guild='42'):
        """Create an event with the exact source physical identity."""
        return MessageUpdated('discord-primary',ref,ContentDocument('updated'),WHEN,
            external_guild_id=guild,author=AuthorRef('discord-primary',actor,'human','Alice'))

    def test_exact_source_edit_queues_author_mutation_without_numeric_router(self):
        """Verified original starter produces one author Update, no numeric ID lookup."""
        asyncio.run(self.handler.handle_platform_message_edit(self._edit(),_Reader()))
        self.service.submit_update.assert_awaited_once_with(object_id='https://bridge.test/objects/1',
            title='hello',body_markdown='updated',edited_timestamp=WHEN,source_message=REF)
        self.router.handle_message_edit.assert_not_awaited()
        self.assertEqual(self.wake.call_count,1)

    def test_same_message_id_other_container_cannot_edit_or_delete_original(self):
        """Same numeric ID in a sibling thread cannot select this canonical AP object."""
        foreign=MessageRef('discord-primary','999','901')
        asyncio.run(self.handler.handle_platform_message_edit(self._edit(foreign),_Reader()))
        asyncio.run(self.handler.handle_platform_message_delete(MessageDeleted('discord-primary',foreign,external_guild_id='42'),_Reader()))
        self.service.submit_update.assert_not_awaited()
        self.service.submit_delete.assert_not_awaited()
        self.router.handle_message_edit.assert_not_awaited()
        self.router.handle_message_delete.assert_not_awaited()

    def test_authorship_or_forum_owner_change_blocks_mutation(self):
        """Wrong author, guild or a rebound surface fails before author service."""
        asyncio.run(self.handler.handle_platform_message_edit(self._edit(actor='66'),_Reader()))
        asyncio.run(self.handler.handle_platform_message_edit(self._edit(guild='43'),_Reader()))
        with self.db.immediate_session() as session:
            surface=session.get(ChannelSurface,self.surface_id)
            surface.local_community_id=None
        asyncio.run(self.handler.handle_platform_message_edit(self._edit(),_Reader()))
        self.service.submit_update.assert_not_awaited()

    def test_exact_source_delete_requires_valid_current_community_owner(self):
        """ID-only delete submits once without numeric routing or content fetch."""
        asyncio.run(self.handler.handle_platform_message_delete(MessageDeleted('discord-primary',REF,external_guild_id='42'),_Reader()))
        self.service.submit_delete.assert_awaited_once_with(object_id='https://bridge.test/objects/1',source_message=REF)
        self.router.handle_message_delete.assert_not_awaited()

    def test_local_comment_uses_exact_source_and_no_post_title(self):
        """One authorized comment edit preserves comment type and parent thread."""
        comment_ref=MessageRef('discord-primary','900','902')
        with self.db.immediate_session() as session:
            post=session.scalar(select(LocalCommunityThread).where(LocalCommunityThread.ap_object_id=='https://bridge.test/objects/1'))
            user=session.scalar(select(User).where(User.discord_user_id=='55'))
            comment=LocalCommunityMessage(local_community_thread_id=post.id,
                ap_activity_id='https://bridge.test/create/comment',
                ap_object_id='https://bridge.test/objects/comment',
                direction='discord_to_ap',
                source_connector_id=self.connector_id,source_channel_surface_id=self.surface_id,
                source_external_container_id='900',source_external_message_id='902')
            session.add_all((comment,
                PublishedActivityObject(actor_username='alice',actor_url=user.actor_url,
                    activity_id=comment.ap_activity_id,object_id=comment.ap_object_id,
                    publication_context='community',community_actor_url='https://bridge.test/communities/town',
                    kind='comment',title=None,body_markdown='previous',
                    source_connector_id=self.connector_id,source_channel_surface_id=self.surface_id,
                    source_external_container_id='900',source_external_message_id='902'),
                MessageMapping(source_platform='discord',source_id='902',
                    activity_id=comment.ap_activity_id,object_id=comment.ap_object_id,
                    actor_url=user.actor_url,community_actor_url='https://bridge.test/communities/town',
                    source_connector_id=self.connector_id,source_channel_surface_id=self.surface_id,
                    source_external_container_id='900',discord_channel_id=100,discord_message_id=902)))
        asyncio.run(self.handler.handle_platform_message_edit(self._edit(comment_ref),_Reader()))
        self.service.submit_update.assert_awaited_once_with(
            object_id='https://bridge.test/objects/comment',title=None,
            body_markdown='updated',edited_timestamp=WHEN,source_message=comment_ref)

    def test_remote_forum_post_uses_exact_connector_and_current_subscription(self):
        """Remote Community source needs the current accepted forum subscription."""
        remote_ref=MessageRef('discord-primary','930','931')
        actor_id='https://remote.test/c/news'
        with self.db.immediate_session() as session:
            sub=ChannelCommunitySubscription(discord_channel_id=200,discord_guild_id=42,
                lemmy_community_actor_id=actor_id,status='accepted')
            user=session.scalar(select(User).where(User.discord_user_id=='55'))
            session.add(sub);session.flush()
            surface=ChannelSurface(connector_id=self.connector_id,external_channel_id='200',
                external_guild_id='42',kind='forum',remote_subscription_id=sub.id)
            session.add(surface);session.flush()
            group=CommunityThreadGroup(community_actor_id=actor_id,
                source_connector_id=self.connector_id,source_channel_surface_id=surface.id,
                source_external_thread_id='930',source_channel_id=200,
                source_thread_id=930,source_starter_message_id=931,
                ap_activity_id='https://bridge.test/create/remote',
                ap_object_id='https://bridge.test/objects/remote')
            session.add_all((group,
                PublishedActivityObject(actor_username='alice',actor_url=user.actor_url,
                    activity_id=group.ap_activity_id,object_id=group.ap_object_id,
                    publication_context='community',community_actor_url=actor_id,
                    kind='post',title='remote title',body_markdown='old',
                    source_connector_id=self.connector_id,source_channel_surface_id=surface.id,
                    source_external_container_id='930',source_external_message_id='931'),
                MessageMapping(source_platform='discord',source_id='931',
                    activity_id=group.ap_activity_id,object_id=group.ap_object_id,
                    actor_url=user.actor_url,community_actor_url=actor_id,
                    source_connector_id=self.connector_id,source_channel_surface_id=surface.id,
                    source_external_container_id='930',discord_channel_id=200,discord_message_id=931)))
        asyncio.run(self.handler.handle_platform_message_edit(self._edit(remote_ref),_Reader()))
        self.service.submit_update.assert_awaited_once()
        self.assertEqual(self.service.submit_update.await_args.kwargs['object_id'],'https://bridge.test/objects/remote')
        with self.db.immediate_session() as session:
            session.scalar(select(ChannelCommunitySubscription).where(ChannelCommunitySubscription.discord_channel_id==200)).status='revoked'
        self.service.submit_delete.reset_mock()
        asyncio.run(self.handler.handle_platform_message_delete(
            MessageDeleted('discord-primary',remote_ref,external_guild_id='42'),_Reader()))
        self.service.submit_delete.assert_not_awaited()

    def test_writer_refuses_source_after_owner_rebound(self):
        """A source verified at ingress cannot use a revoked forum claim at commit."""
        with self.db.session() as session:
            from src.db.repositories.activitypub_objects import resolve_community_mutation_source
            self.assertIsNotNone(resolve_community_mutation_source(
                session,REF,external_guild_id='42',actor_external_id='55'))
        with self.db.immediate_session() as session:
            session.get(ChannelSurface,self.surface_id).local_community_id=None
        self.assertEqual(self.db.content_mutations.record_desired_update(
            object_id='https://bridge.test/objects/1',title='hello',
            body_markdown='wrong after revoke',edited_timestamp=WHEN,
            source_message=REF),'ineligible')
        self.assertEqual(self.db.content_mutations.record_desired_delete(
            object_id='https://bridge.test/objects/1',source_message=REF),'ineligible')
        with self.db.session() as session:
            saved=session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.object_id=='https://bridge.test/objects/1'))
            self.assertEqual(saved.desired_kind,'none')
            self.assertEqual(saved.body_markdown,'original')

    def test_writer_rejects_other_container_even_when_id_matches(self):
        """An exact Community mutation cannot be reserved using a sibling thread."""
        different=MessageRef('discord-primary','other-thread','901')
        self.assertEqual(self.db.content_mutations.record_desired_update(
            object_id='https://bridge.test/objects/1',title='hello',
            body_markdown='forged',edited_timestamp=WHEN,
            source_message=different),'ineligible')
        with self.db.session() as session:
            saved=session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.object_id=='https://bridge.test/objects/1'))
            self.assertEqual(saved.desired_kind,'none')

    def test_deferred_partial_edit_fetches_from_immediate_thread_not_parent_forum(self):
        """Post-restart reconciliation must read the original physical container."""
        from src.user_actor_delivery import UserActorDeliveryService

        self.assertEqual(self.db.content_mutations.request_discord_reconciliation_by_source_ref(
            source=REF,error='lost read'),'accepted')
        calls=[]
        class Reader:
            """An exact state read with no Discord SDK dependency."""
            async def fetch_message_state(self, channel_id, message_id):
                """Return a verified edit from the requested physical container."""
                calls.append((channel_id,message_id))
                return ('present',SimpleNamespace(content='author update',edited_at=WHEN))
        service=object.__new__(UserActorDeliveryService)
        service.database=self.db
        service.discord_message_reader=Reader()
        asyncio.run(service.drain_reconciliations())
        self.assertEqual(calls,[(900,901)])
        with self.db.session() as session:
            published=session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.object_id=='https://bridge.test/objects/1'))
            self.assertFalse(published.discord_reconciliation_needed)
            self.assertEqual(published.desired_kind,'update')

    def test_missing_partial_edit_is_not_an_authenticated_community_delete(self):
        """A 404 during Update read does not prove the source author deleted it."""
        from src.user_actor_delivery import UserActorDeliveryService
        self.assertEqual(self.db.content_mutations.request_discord_reconciliation_by_source_ref(
            source=REF,error='lost read'),'accepted')
        class Reader:
            """Simulate Discord returning a cacheless missing source."""
            async def fetch_message_state(self, channel_id, message_id):
                """A 404 can also reflect permissions and is not author intent."""
                return ('missing',None)
        service=object.__new__(UserActorDeliveryService)
        service.database=self.db
        service.discord_message_reader=Reader()
        asyncio.run(service.drain_reconciliations())
        with self.db.session() as session:
            published=session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.object_id=='https://bridge.test/objects/1'))
            self.assertTrue(published.discord_reconciliation_needed)
            self.assertEqual(published.desired_kind,'none')

    def test_rebound_forum_never_fetches_deferred_partial_edit(self):
        """Recovery validates the owner before physical I/O or canonical mutation."""
        from src.user_actor_delivery import UserActorDeliveryService
        self.assertEqual(self.db.content_mutations.request_discord_reconciliation_by_source_ref(
            source=REF,error='lost read'),'accepted')
        with self.db.immediate_session() as session:
            session.get(ChannelSurface,self.surface_id).local_community_id=None
        calls=[]
        class Reader:
            """Fail if a stale forum owner reaches the Discord read boundary."""
            async def fetch_message_state(self, channel_id, message_id):
                """A revoked source must never be fetched or published."""
                calls.append((channel_id,message_id))
                raise AssertionError('forbidden read')
        service=object.__new__(UserActorDeliveryService)
        service.database=self.db
        service.discord_message_reader=Reader()
        asyncio.run(service.drain_reconciliations())
        self.assertEqual(calls,[])
        with self.db.session() as session:
            row=session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.object_id=='https://bridge.test/objects/1'))
            self.assertTrue(row.discord_reconciliation_needed)
            self.assertEqual(row.desired_kind,'none')

    def test_personal_feed_reconciliation_keeps_its_legacy_channel_projection(self):
        """The separate Personal Feed reader is not retargeted to a forum thread."""
        with self.db.immediate_session() as session:
            user=session.scalar(select(User).where(User.discord_user_id=='55'))
            row=PublishedActivityObject(actor_username='alice',actor_url=user.actor_url,
                activity_id='https://bridge.test/create/pf',object_id='https://bridge.test/objects/pf',
                publication_context='personal_feed',community_actor_url=None,
                discord_guild_id=42,discord_channel_id=123,discord_message_id=777,
                kind='post',title=None,body_markdown='old',
                source_connector_id=self.connector_id,
                discord_reconciliation_needed=True)
            session.add(row)
        due=self.db.content_mutations.due_reconciliations(limit=10)
        self.assertEqual(due,[(123,777,'https://bridge.test/objects/pf',None,None)])

    def test_partial_edit_error_sets_reconciliation_on_only_exact_source(self):
        """Reconciliation cannot poison a same-ID different physical message."""
        partial=MessageUpdated('discord-primary',REF,ContentDocument('stale'),
            external_guild_id='42')
        asyncio.run(self.handler.handle_platform_message_edit(
            partial,_Reader(failure=PlatformOperationError('unavailable','read lost'))))
        with self.db.session() as session:
            own=session.scalar(select(PublishedActivityObject).where(PublishedActivityObject.object_id=='https://bridge.test/objects/1'))
            self.assertTrue(own.discord_reconciliation_needed)
            self.assertEqual(own.discord_reconciliation_last_error,'read lost')
        self.service.submit_update.assert_not_awaited()
        self.assertEqual(self.wake.call_count,1)

    def test_partial_read_wrong_author_does_not_edit(self):
        """A forged partial refetch cannot bypass published author provenance."""
        partial=MessageUpdated('discord-primary',REF,external_guild_id='42')
        view=MessageView(REF,AuthorRef('discord-primary','66','human','Other'),
            ContentDocument('forged'),edited_at=WHEN)
        asyncio.run(self.handler.handle_platform_message_edit(partial,_Reader(view=view)))
        self.service.submit_update.assert_not_awaited()

    def test_legacy_personal_feed_message_remains_separate_from_community(self):
        """An untyped Personal Feed source is still owned by its legacy delivery."""
        with self.db.immediate_session() as session:
            user=session.scalar(select(User).where(User.discord_user_id=='55'))
            session.add(MessageMapping(source_platform='discord',source_id='777',
                activity_id='https://bridge.test/create/personal',object_id='https://bridge.test/objects/personal',
                actor_url=user.actor_url,community_actor_url=None,
                publication_context='personal_feed',source_connector_id=self.connector_id,discord_guild_id=42,discord_channel_id=123,discord_message_id=777))
        event=MessageDeleted('discord-primary',MessageRef('discord-primary','123','777'),
            external_guild_id='42')
        asyncio.run(self.handler.handle_platform_message_delete(event,_Reader()))
        self.service.submit_delete.assert_awaited_once_with(object_id='https://bridge.test/objects/personal')
        self.router.handle_message_delete.assert_not_awaited()
