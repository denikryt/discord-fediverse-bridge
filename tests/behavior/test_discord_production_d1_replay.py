"""Production Discord Gateway replay across registration, publishing and cold restart.

Only the external Discord SDK channel/message reads and Fedify renderer are
substituted. The built application router, adapter, repositories and SQLite
claims are never replaced with hand-inserted source mappings.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from bridge_test_support.community_events import configure_renderer
from bridge_test_support.runtime import runtime_settings
from src.models import InboundConversationObject, MessageMapping, PublishedActivityObject
from src.platform_types import MessageRef

try:
    import discord
except ImportError:
    discord = None


class _ObservedForum:
    """Observed Discord forum identity at the SDK read boundary."""

    def __init__(self, guild: object, forum_id: int = 100) -> None:
        """Expose the immutable forum/guild relationship to the adapter."""
        self.id = forum_id
        self.name = 'Registered community'
        self.guild = guild
        self.flags = SimpleNamespace(require_tag=False)


class _ObservedText:
    """SDK text channel with the production protocol channel discriminator."""

    def __init__(self, guild: object, channel_id: int = 400) -> None:
        """Expose a real personal-feed surface, not a forum or thread."""
        self.id = channel_id
        self.guild = guild
        self.name = 'Personal feed'
        self.type = discord.ChannelType.text

    def is_news(self) -> bool:
        """Report the observed ordinary-text channel kind."""
        return False


class _ObservedThread:
    """Observed Discord thread retaining the exact parent, starter and replies."""

    def __init__(self, *, thread_id: int, forum: _ObservedForum | _ObservedText, starter: object) -> None:
        """Make the SDK readback agree with the event's physical container."""
        self.id = thread_id
        self.parent_id = forum.id
        self.parent = forum
        self.guild = forum.guild
        self.name = 'Forum discussion'
        self.owner_id = 123
        self.starter_message = starter
        self.messages: dict[int, object] = ({starter.id: starter}
                                            if starter is not None else {})

    async def fetch_message(self, identifier: int) -> object:
        """Return only the addressed message, never a guessed starter."""
        return self.messages[int(identifier)]


def _human_message(thread: _ObservedThread, identifier: int, *, reply_to: int | None = None) -> object:
    """Construct one timestamped human SDK event in a checked physical thread."""
    author = SimpleNamespace(id=123, bot=False, name='alice', display_name='Alice')
    return SimpleNamespace(
        id=identifier, channel=thread, guild=thread.guild, author=author,
        content=f'body-{identifier}', created_at=datetime(2026, 11, 18, tzinfo=UTC),
        attachments=(), webhook_id=None, type='default',
        reference=(SimpleNamespace(message_id=reply_to, channel_id=thread.id)
                   if reply_to is not None else None),
    )


@unittest.skipIf(discord is None, 'production Discord callbacks require discord.py')
class DiscordProductionD1Replay(unittest.IsolatedAsyncioTestCase):
    """The real app graph handles repeated human messages and preserves exact IDs."""

    async def asyncSetUp(self) -> None:
        """Construct a new deployment and real domain owners, no preseeded source rows."""
        from src.app import build_runtime
        from src.local_communities.service import LocalCommunityService
        from src.discord_adapter import DiscordAdapter
        from src.discord_bot import BridgeBot

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = runtime_settings(Path(self.temporary.name))
        self.guild = SimpleNamespace(id=10)
        self.forum = _ObservedForum(self.guild)
        self.thread = _ObservedThread(thread_id=200, forum=self.forum, starter=None)
        self.thread.starter_message = _human_message(self.thread, 200)
        self.thread.messages = {200: self.thread.starter_message}
        self.channels = {self.forum.id: self.forum, self.thread.id: self.thread}
        # Fake only the SDK type-identity boundary; real app construction and
        # the production adapter's validation and normalization still run.
        self.sdk = SimpleNamespace(ForumChannel=_ObservedForum,
                                   Thread=_ObservedThread, TextChannel=_ObservedText)
        self.sdk_patch = patch.object(DiscordAdapter, '_sdk', staticmethod(lambda: self.sdk))
        self.thread_patch = patch.object(discord, 'Thread', _ObservedThread)
        self.text_patch = patch.object(discord, 'TextChannel', _ObservedText)
        self.sdk_patch.start()
        self.thread_patch.start()
        self.text_patch.start()
        self.addCleanup(self.text_patch.stop)
        self.addCleanup(self.sdk_patch.stop)
        self.addCleanup(self.thread_patch.stop)
        self.runtime = build_runtime(self.settings)
        self._attach_external_boundaries(self.runtime)
        self.runtime.registration_service.keypair_generator = lambda: ('public', 'private')
        result, actor = self.runtime.registration_service.create_or_get_user(
            discord_user_id='123', requested_username='alice',
        )
        self.assertEqual(result, 'created')
        self.actor_url = actor.actor_url
        created = LocalCommunityService(
            database=self.runtime.database, base_url=self.settings.normalized_fedify_origin,
            keypair_generator=lambda: ('public', 'private'),
        ).create_local_community(
            discord_guild_id=10, discord_forum_channel_id=100,
            slug='testforum', name='Test forum', description=None,
            created_by_discord_user_id='123',
        )
        self.community_actor_url = created.actor_url

    def _attach_external_boundaries(self, runtime: object) -> None:
        """Keep SDK reads and Fedify rendering deterministic across cold rebuilds."""
        runtime.bot._connection.user = SimpleNamespace(id=999, bot=True)
        runtime.bot.get_channel = lambda identifier: self.channels.get(int(identifier))
        runtime.bot.fetch_channel = AsyncMock(side_effect=lambda identifier: self.channels[int(identifier)])
        runtime.fedify_gateway.render_community = AsyncMock()
        configure_renderer(runtime.fedify_gateway, runtime.database)

    async def asyncTearDown(self) -> None:
        """Close all runtime owners before a subsequent test reuses the SQLite path."""
        await self.runtime.close()

    def _snapshot(self) -> tuple[tuple, tuple]:
        """Read canonical scoped tuples and stable public parent links from SQLite."""
        with self.runtime.database.session() as session:
            objects = session.scalars(select(PublishedActivityObject).order_by(
                PublishedActivityObject.id,
            )).all()
            mappings = session.scalars(select(MessageMapping).order_by(
                MessageMapping.id,
            )).all()
            return (
                tuple((row.kind, row.object_id, row.in_reply_to_object_id,
                       row.source_connector_id, row.source_external_container_id,
                       row.source_external_message_id) for row in objects),
                tuple((row.object_id, row.source_external_container_id,
                       row.source_id) for row in mappings),
            )

    async def test_thread_replay_after_cold_runtime_rebuild_preserves_single_source(self) -> None:
        """One human starter stays unique through duplicate, close, rebuild and replay."""
        await self.runtime.bot.on_thread_create(self.thread)
        first = self._snapshot()
        self.assertEqual(len(first[0]), 1)
        self.assertEqual(len(first[1]), 1)
        self.assertEqual(first[0][0][4:], ('200', '200'))
        self.assertEqual(first[0][0][0], 'post')
        self.assertEqual(self.runtime.fedify_gateway.render_community.await_count, 1)
        await self.runtime.bot.on_thread_create(self.thread)
        self.assertEqual(self._snapshot(), first)
        self.assertEqual(self.runtime.fedify_gateway.render_community.await_count, 1)
        await self.runtime.close()
        from src.app import build_runtime
        self.runtime = build_runtime(self.settings)
        self._attach_external_boundaries(self.runtime)
        await self.runtime.bot.on_thread_create(self.thread)
        self.assertEqual(self._snapshot(), first)
        self.runtime.fedify_gateway.render_community.assert_not_awaited()
        source = self.runtime.database.activitypub_objects.get_by_source_ref(
            MessageRef('discord-primary', '200', '200'),
        )
        self.assertIsNotNone(source)
        self.assertEqual(source.object_id, first[0][0][1])

    async def test_remote_subscription_denies_before_accept_then_routes_reply_after_accept(self) -> None:
        """Pending follow blocks writes; accepted subscription owns one routed post/reply."""
        from src.activitypub_handlers import dispatch_activitypub_event
        from src.activitypub_models import FollowLifecycleEvent
        from src.activitypub_identity_urls import build_service_follow_activity_id

        remote_forum = _ObservedForum(self.guild, forum_id=101)
        thread = _ObservedThread(thread_id=210, forum=remote_forum, starter=None)
        thread.starter_message = _human_message(thread, 210)
        thread.messages[210] = thread.starter_message
        self.channels.update({101: remote_forum, 210: thread})
        remote_actor = 'https://remote.test/c/hackers'
        follow_id = build_service_follow_activity_id(
            origin=self.settings.normalized_fedify_origin, identifier='bridge',
            activity_id='subscription-101',
        )
        # The actual Accept handler needs the shared bridge Follow owner, not
        # merely a channel subscription; both durable rows belong to routing.
        self.runtime.database.bridge_actor_follows.create_bridge_actor_follow(
            community_actor_id=remote_actor, follow_activity_id=follow_id,
            community_inbox_url=f'{remote_actor}/inbox', status='pending',
        )
        self.runtime.database.remote_subscriptions.create_subscription(
            discord_channel_id=101, discord_guild_id=10,
            lemmy_community_actor_id=remote_actor, lemmy_community_name='hackers',
            lemmy_community_id=777, community_handle='!hackers@remote.test',
            community_inbox_url=f'{remote_actor}/inbox',
            follow_activity_id=follow_id, status='pending',
        )
        await self.runtime.bot.on_thread_create(thread)
        self.assertEqual(self._snapshot(), ((), ()))
        self.runtime.fedify_gateway.render_community.assert_not_awaited()
        accepted = await dispatch_activitypub_event(
            FollowLifecycleEvent(
                event_type='follow.accepted',
                delivery_id='https://remote.test/activities/accept/subscription-101',
                occurred_at=datetime(2026, 11, 18, tzinfo=UTC),
                community_actor_id=remote_actor, actor_id=remote_actor,
                object={'follow_activity_id': follow_id},
            ), self.runtime,
        )
        self.assertEqual(accepted.status, 'processed')
        self.assertEqual(self.runtime.database.remote_subscriptions.get_subscription_by_channel(
            101).status, 'accepted')
        await self.runtime.bot.on_thread_create(thread)
        parent = _human_message(thread, 211, reply_to=210)
        thread.messages[parent.id] = parent
        await self.runtime.bot.on_message(parent)
        # Duplicate source notices do not create a second canonical object.
        await self.runtime.bot.on_thread_create(thread)
        await self.runtime.bot.on_message(parent)
        objects, mappings = self._snapshot()
        self.assertEqual(len(objects), 2)
        self.assertEqual(len(mappings), 2)
        by_source = {row[5]: row for row in objects}
        self.assertEqual(by_source['211'][2], by_source['210'][1])
        self.assertEqual({row[4] for row in objects}, {'210'})
        self.assertEqual(self.runtime.fedify_gateway.render_community.await_count, 2)

    async def test_subscribe_command_accept_and_replay_isolated_from_other_subscription(self) -> None:
        """The registered slash callback owns Follow; only Accept enables its forum."""
        from src.activitypub_handlers import dispatch_activitypub_event
        from src.activitypub_models import FollowLifecycleEvent
        from src.activitypub_identity_urls import build_service_follow_activity_id
        from src.commands import subscribe
        from src.community_discovery import ResolvedCommunity
        from src.models import BridgeActorFollow, ChannelCommunitySubscription

        class RecordingTree:
            """Collect the actual slash callback instead of bypassing command registration."""

            def __init__(self) -> None:
                """Retain command callbacks registered by the production adapter."""
                self.commands: dict[str, object] = {}

            def command(self, *, name: str, description: str) -> object:
                """Preserve command metadata while leaving its callback unmodified."""
                def capture(callback: object) -> object:
                    """Expose one decorated production callback for the scenario."""
                    self.commands[name] = callback
                    return callback
                return capture

        forum = _ObservedForum(self.guild, forum_id=101)
        unrelated = _ObservedForum(self.guild, forum_id=102)
        self.channels.update({101: forum, 102: unrelated})
        forum.mention, unrelated.mention = '<#101>', '<#102>'
        subscriptions = {
            101: 'https://remote.test/c/hackers',
            102: 'https://remote.test/c/unrelated',
        }
        tree = RecordingTree()
        subscribe.register(
            tree, self.runtime.database, self.runtime.fedify_gateway,
            self.settings, self.runtime.bridge_policy_service,
        )
        callback = tree.commands['subscribe-community']
        follow_calls: list[str] = []

        async def resolve(_settings: object, *, instance_domain: object,
                          community_value: str, **_kwargs: object) -> ResolvedCommunity:
            """Resolve only the remote actor supplied by the moderator at the network edge."""
            self.assertIn(community_value, subscriptions.values())
            return ResolvedCommunity(
                source='remote_lemmy', actor_id=community_value,
                name=community_value.rsplit('/', 1)[-1], numeric_id=777,
                handle='!' + community_value.rsplit('/', 1)[-1] + '@remote.test',
            )

        async def follow(actor_url: str) -> object:
            """Capture one genuine Follow submission without a Fedify network dependency."""
            follow_calls.append(actor_url)
            return SimpleNamespace(
                community_actor_url=actor_url,
                community_inbox_url=actor_url + '/inbox',
                follow_activity_id=build_service_follow_activity_id(
                    origin=self.settings.normalized_fedify_origin, identifier='bridge',
                    activity_id='subscription-' + actor_url.rsplit('/', 1)[-1],
                ),
            )

        def interaction() -> object:
            """Represent a registered moderator performing a Discord slash command."""
            return SimpleNamespace(
                guild=self.guild, guild_id=10,
                user=SimpleNamespace(id=123, guild_permissions=SimpleNamespace(manage_guild=True)),
                response=SimpleNamespace(send_message=AsyncMock()),
            )

        async def accepted(actor_url: str, delivery_suffix: str) -> object:
            """Send the production inbound Accept handler one actor-owned Follow ID."""
            result = await dispatch_activitypub_event(FollowLifecycleEvent(
                event_type='follow.accepted',
                delivery_id=f'https://remote.test/actors/system/activities/accept/{delivery_suffix}',
                occurred_at=datetime(2026, 11, 18, tzinfo=UTC),
                community_actor_id=actor_url, actor_id=actor_url,
                object={'follow_activity_id': self.runtime.database.bridge_actor_follows
                        .get_bridge_actor_follow(actor_url).follow_activity_id},
            ), self.runtime)
            return result

        # Subscribe a distinct forum first so the tested destination cannot
        # accidentally pass by being the only accepted subscription.
        with (patch('src.commands.subscribe.resolve_selected_community', side_effect=resolve),
              patch.object(self.runtime.fedify_gateway, 'follow_community', side_effect=follow),
              patch.object(self.runtime.bot, 'fetch_user', new_callable=AsyncMock, create=True) as notify):
            user = SimpleNamespace(send=AsyncMock())
            notify.return_value = user
            for channel in (unrelated, forum):
                await callback(interaction(), community=subscriptions[channel.id],
                               channel=channel, instance_domain=None)
            self.assertEqual(follow_calls, list(subscriptions.values())[::-1])
            self.assertEqual({row.status for row in
                self.runtime.database.remote_subscriptions.list_subscriptions()}, {'pending'})
            self.assertEqual((await accepted(subscriptions[102], 'other')).status, 'processed')
            self.assertEqual(self.runtime.database.remote_subscriptions
                             .get_subscription_by_channel(102).status, 'accepted')

            # The desired channel is still pending although another actor is
            # accepted. Pending is never permission to publish a human post.
            thread = _ObservedThread(thread_id=210, forum=forum, starter=None)
            thread.starter_message = _human_message(thread, 210)
            thread.messages[210] = thread.starter_message
            self.channels[210] = thread
            await self.runtime.bot.on_thread_create(thread)
            self.assertEqual(self._snapshot(), ((), ()))
            self.runtime.fedify_gateway.render_community.assert_not_awaited()
            self.assertEqual((await accepted(subscriptions[101], 'target')).status, 'processed')
            self.assertEqual((await accepted(subscriptions[101], 'target-repeat')).status, 'skipped')
            self.assertEqual(self.runtime.database.remote_subscriptions
                             .get_subscription_by_channel(101).status, 'accepted')
            await self.runtime.bot.on_thread_create(thread)
            reply = _human_message(thread, 211, reply_to=210)
            thread.messages[211] = reply
            await self.runtime.bot.on_message(reply)
            await self.runtime.bot.on_message(reply)
            await self.runtime.bot.on_thread_create(thread)
            objects, copies = self._snapshot()
            self.assertEqual(len(objects), 2)
            self.assertEqual(len(copies), 2)
            root, comment = objects
            self.assertEqual(comment[2], root[1])
            self.assertEqual({row[4] for row in objects}, {'210'})
            self.assertEqual({row[5] for row in objects}, {'210', '211'})
            with self.runtime.database.session() as session:
                published = session.scalars(select(PublishedActivityObject)).all()
                self.assertEqual({row.community_actor_url for row in published},
                                 {subscriptions[101]})
                self.assertNotIn(subscriptions[102],
                                 {row.community_actor_url for row in published})
            self.assertEqual(self.runtime.fedify_gateway.render_community.await_count, 2)
            with self.runtime.database.session() as session:
                self.assertEqual(len(session.scalars(select(BridgeActorFollow)).all()), 2)
                self.assertEqual(len(session.scalars(select(ChannelCommunitySubscription)).all()), 2)
            self.assertEqual(follow_calls.count(subscriptions[101]), 1)
            self.assertEqual(follow_calls.count(subscriptions[102]), 1)

    async def test_personal_feed_inbound_discussion_and_human_reply_do_not_enter_forum(self) -> None:
        """A user Note, remote reply and adopted discussion keep their own actor/parent."""
        from src.activitypub_handlers import dispatch_activitypub_event
        from src.activitypub_models import ActivityPubEvent, ActivityPubObject, ActivityPubTarget
        from src.fedify_gateway_client import RenderPersonalFeedResult

        channel = _ObservedText(self.guild)
        self.channels[channel.id] = channel
        bound = self.runtime.bot.event_router.personal_feed_service.bind_or_move('123', 10, 400)
        self.assertEqual(bound.status, 'bound')
        self.runtime.registration_service.create_or_get_user(
            discord_user_id='124', requested_username='bob',
        )

        async def render_personal(request: object) -> RenderPersonalFeedResult:
            """Render the exact IDs at the genuine Fedify outbound boundary."""
            return RenderPersonalFeedResult(
                request.activity_id, request.object_id, request.published_at,
                {'id': request.activity_id, 'type': 'Create',
                 'actor': f'{self.settings.normalized_fedify_origin}/actors/users/{request.actor_username}',
                 'object': {'id': request.object_id, 'type': 'Note',
                            'content': request.body_markdown,
                            'inReplyTo': request.in_reply_to_object_id}},
            )

        self.runtime.fedify_gateway.render_personal_feed = AsyncMock(side_effect=render_personal)
        root = SimpleNamespace(
            id=401, channel=channel, guild=self.guild,
            author=SimpleNamespace(id=123, bot=False, display_name='Alice'),
            content='personal root', created_at=datetime(2026, 11, 18, tzinfo=UTC),
            attachments=(), webhook_id=None, type='default', reference=None,
        )
        await self.runtime.bot.on_message(root)
        await self.runtime.bot.on_message(root)
        objects, mappings = self._snapshot()
        self.assertEqual(len(objects), 1)
        self.assertEqual(len(mappings), 1)
        # Personal Feed still persists its Discord channel in legacy projections.
        self.assertEqual(objects[0][5], '401')
        with self.runtime.database.session() as session:
            published = session.scalar(select(PublishedActivityObject).where(
                PublishedActivityObject.object_id == objects[0][1],
            ))
            self.assertEqual(published.discord_channel_id, 400)
        self.assertEqual(self.runtime.fedify_gateway.render_personal_feed.await_count, 1)
        root_ap_id = objects[0][1]

        remote = ActivityPubEvent(
            event_type='comment.created', delivery_id='remote-delivery-1',
            occurred_at=datetime(2026, 11, 18, tzinfo=UTC),
            actor_id='https://remote.test/users/carol',
            target=ActivityPubTarget(kind='personal_feed', root_object_id=root_ap_id),
            object=ActivityPubObject(
                kind='comment', ap_id='https://remote.test/objects/carol-reply',
                parent_ap_id=root_ap_id, published_at=datetime(2026, 11, 18, tzinfo=UTC),
                author_name='Carol', attributed_to_actor_id='https://remote.test/users/carol',
                body_markdown='remote personal reply',
            ),
        )
        self.assertEqual((await dispatch_activitypub_event(remote, self.runtime)).status, 'processed')
        self.assertIsNotNone(self.runtime.database.personal_feed_conversations.get_inbound_object(
            remote.object.ap_id,
        ))
        # Transport replay and a different delivery ID for the same AP object
        # must independently leave the canonical inbound object unique.
        await dispatch_activitypub_event(remote, self.runtime)
        await dispatch_activitypub_event(
            remote.model_copy(update={'delivery_id': 'remote-delivery-2'}), self.runtime,
        )
        with self.runtime.database.session() as session:
            self.assertEqual(len(session.scalars(select(InboundConversationObject)).all()), 1)
        # The discussion's thread ID equals the physical root message ID.
        # It belongs to the text channel, not to the active forum fixture.
        discussion = _ObservedThread(thread_id=401, forum=channel, starter=None)
        self.channels[discussion.id] = discussion
        await self.runtime.bot.on_thread_create(discussion)
        self.assertTrue(self.runtime.bot.event_router.is_personal_feed_thread(401))
        reply = _human_message(discussion, 402)
        reply.author = SimpleNamespace(id=124, bot=False, display_name='Bob')
        discussion.messages[reply.id] = reply
        await self.runtime.bot.on_message(reply)
        await self.runtime.bot.on_message(reply)
        final, copies = self._snapshot()
        self.assertEqual(len(final), 2)
        self.assertEqual(len(copies), 2)
        by_source = {row[5]: row for row in final}
        self.assertEqual(by_source['402'][2], root_ap_id)
        self.assertIn('/actors/users/bob/', by_source['402'][1])
        with self.runtime.database.session() as session:
            self.assertEqual({row.discord_channel_id for row in session.scalars(
                select(PublishedActivityObject).where(
                    PublishedActivityObject.publication_context == 'personal_feed',
                ),
            ).all()}, {400})
        self.assertEqual(self.runtime.fedify_gateway.render_personal_feed.await_count, 2)
        self.runtime.fedify_gateway.render_community.assert_not_awaited()

    async def test_remote_personal_reply_worker_creates_and_records_discussion_before_echo(self) -> None:
        """Inbound AP reply wakes the real worker, creates one discussion and ignores its echo."""
        from src.activitypub_handlers import dispatch_activitypub_event
        from src.activitypub_models import ActivityPubEvent, ActivityPubObject, ActivityPubTarget
        from src.fedify_gateway_client import RenderPersonalFeedResult
        from src.models import InboundDiscordDelivery, PersonalFeedThreadMapping
        from src.platform_types import (
            ChannelRef, ChannelView, CreatedMessage, MessageRef,
            PlatformOperationError, ThreadRef,
        )

        channel = _ObservedText(self.guild)
        self.channels[channel.id] = channel
        self.assertEqual(
            self.runtime.bot.event_router.personal_feed_service.bind_or_move('123', 10, 400).status,
            'bound',
        )
        self.runtime.registration_service.create_or_get_user(
            discord_user_id='124', requested_username='bob',
        )

        async def render(request: object) -> RenderPersonalFeedResult:
            """Render the real publication's actor-scoped IDs at the gateway boundary."""
            return RenderPersonalFeedResult(
                request.activity_id, request.object_id, request.published_at,
                {'id': request.activity_id, 'type': 'Create',
                 'object': {'id': request.object_id, 'type': 'Note',
                            'content': request.body_markdown}},
            )

        self.runtime.fedify_gateway.render_personal_feed = AsyncMock(side_effect=render)
        root = SimpleNamespace(
            id=401, channel=channel, guild=self.guild,
            author=SimpleNamespace(id=123, bot=False, display_name='Alice'),
            content='root with remote discussion', created_at=datetime(2026, 11, 18, tzinfo=UTC),
            attachments=(), webhook_id=None, type='default', reference=None,
        )
        await self.runtime.bot.on_message(root)
        root_ap_id = self._snapshot()[0][0][1]
        worker = self.runtime.personal_feed_discord_delivery_worker
        self.assertIsNotNone(worker)
        # The adapter's four SDK I/O methods are the sole fake. The real
        # scheduler, persistence, admission and incoming AP router are active.
        physical: list[tuple[str, str, str | None]] = []
        thread_creates: list[str] = []
        observed_thread = _ObservedThread(thread_id=401, forum=channel, starter=None)

        async def fetch_channel(ref: ChannelRef) -> ChannelView:
            """Resolve the observed text root through the physical adapter port."""
            self.assertEqual(ref.external_channel_id, '400')
            return ChannelView(ChannelRef('discord-primary', '400', 'text', '10'), 'feed')

        async def fetch_thread(ref: ThreadRef) -> object:
            """A discussion is initially absent; no caller may invent its receipt."""
            self.assertEqual(ref.external_thread_id, '401')
            if not thread_creates:
                raise PlatformOperationError('not_found', 'no discussion yet')
            from src.platform_types import ThreadView
            return ThreadView(ref, 'discussion', kind='message')

        async def create_reply_thread(ref: MessageRef, *, title: str, attempt: object) -> ThreadRef:
            """Persist one physical SDK Create tied to the root message identity."""
            self.assertEqual(ref, MessageRef('discord-primary', '400', '401'))
            self.assertTrue(title)
            thread_creates.append(attempt.key)
            self.channels[401] = observed_thread
            return ThreadRef(ChannelRef('discord-primary', '400', 'text', '10'), '401')

        async def send_message(
            destination: ThreadRef, content: object, *, reply_to: object,
            presentation: object, attempt: object,
        ) -> CreatedMessage:
            """Retain exact SDK side effects and a typed bot-owned acknowledgement."""
            self.assertEqual(destination.external_thread_id, '401')
            self.assertIsNone(reply_to)
            physical.append((destination.external_thread_id, content.body, reply_to))
            return CreatedMessage(MessageRef('discord-primary', '401', '403'), 'bot')

        with (patch.object(worker.adapter, 'fetch_channel', side_effect=fetch_channel),
              patch.object(worker.adapter, 'fetch_thread', side_effect=fetch_thread),
              patch.object(worker.adapter, 'create_reply_thread', side_effect=create_reply_thread),
              patch.object(worker.adapter, 'send_message', side_effect=send_message)):
            worker.start()
            try:
                event = ActivityPubEvent(
                    event_type='comment.created', delivery_id='remote-worker-delivery-1',
                    occurred_at=datetime(2026, 11, 18, tzinfo=UTC),
                    actor_id='https://remote.test/users/carol',
                    target=ActivityPubTarget(kind='personal_feed', root_object_id=root_ap_id),
                    object=ActivityPubObject(
                        kind='comment', ap_id='https://remote.test/objects/worker-reply',
                        parent_ap_id=root_ap_id, published_at=datetime(2026, 11, 18, tzinfo=UTC),
                        author_name='Carol', attributed_to_actor_id='https://remote.test/users/carol',
                        body_markdown='remote worker reply',
                    ),
                )
                self.assertEqual((await dispatch_activitypub_event(event, self.runtime)).status,
                                 'processed')

                async def wait_for_receipt() -> None:
                    """Wait on persisted worker state instead of assuming an SDK acknowledgement."""
                    while True:
                        with self.runtime.database.session() as session:
                            row = session.scalar(select(InboundDiscordDelivery))
                            if row is not None and row.state == 'delivered':
                                return
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(wait_for_receipt(), timeout=3)
                self.assertEqual(thread_creates, ['personal-feed-root-thread:' + root_ap_id])
                self.assertEqual(len(physical), 1)
                with self.runtime.database.session() as session:
                    rows = session.scalars(select(PersonalFeedThreadMapping)).all()
                    receipts = session.scalars(select(InboundDiscordDelivery)).all()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0].external_thread_id, '401')
                    self.assertEqual(len(receipts), 1)
                    self.assertEqual(receipts[0].external_thread_id, '401')
                    self.assertEqual(receipts[0].external_message_id, '403')
                    self.assertEqual(receipts[0].state, 'delivered')
                # Identity is connector+thread+message, never a naked Discord ID
                # or a reference into another root's physical conversation.
                parent_lookup = self.runtime.database.personal_feed_conversations
                self.assertEqual(parent_lookup.get_remote_parent_for_discord_reply(
                    root_object_id=root_ap_id, thread_id=401, message_id=403,
                ), event.object.ap_id)
                self.assertIsNone(parent_lookup.get_remote_parent_for_discord_reply(
                    root_object_id=root_ap_id, thread_id=999, message_id=403,
                ))
                self.assertIsNone(parent_lookup.get_remote_parent_for_discord_reply(
                    root_object_id='https://bridge.example/objects/foreign-root',
                    thread_id=401, message_id=403,
                ))
                # An already-proven physical ID is still a legitimate parent
                # while a later remote Update awaits Discord re-rendering.
                await worker.stop()
                updated = parent_lookup.admit_update(
                    object_id=event.object.ap_id,
                    activity_actor_id=event.actor_id,
                    attributed_to_actor_id=event.actor_id,
                    content_markdown='new content',
                    updated_at=datetime(2026, 11, 18, 1, tzinfo=UTC),
                    source_activity={'type': 'Update'},
                )
                self.assertEqual(updated.status, 'accepted')
                with self.runtime.database.session() as session:
                    self.assertEqual(session.scalar(select(InboundDiscordDelivery)).state,
                                     'ready')
                self.assertEqual(parent_lookup.get_remote_parent_for_discord_reply(
                    root_object_id=root_ap_id, thread_id=401, message_id=403,
                ), event.object.ap_id)
                # The physical Gateway echo is not a new community publication.
                before = self._snapshot()
                await self.runtime.bot.on_thread_create(observed_thread)
                self.assertEqual(self._snapshot(), before)
                self.assertTrue(self.runtime.bot.event_router.is_personal_feed_thread(401))
                human = _human_message(observed_thread, 404, reply_to=403)
                human.author = SimpleNamespace(id=124, bot=False, display_name='Bob')
                observed_thread.messages[404] = human
                await self.runtime.bot.on_message(human)
                await self.runtime.bot.on_message(human)
                objects, _copies = self._snapshot()
                self.assertEqual(len(objects), 2)
                self.assertEqual(objects[1][2], event.object.ap_id)
                self.assertEqual(objects[1][5], '404')
                self.assertEqual(len(thread_creates), 1)
                self.assertEqual(len(physical), 1)
                self.runtime.fedify_gateway.render_community.assert_not_awaited()
            finally:
                await worker.stop()

    async def test_reply_duplicate_and_nested_reply_keep_immediate_parent(self) -> None:
        """The real Gateway callback publishes each reply once with its exact AP parent."""
        await self.runtime.bot.on_thread_create(self.thread)
        first = _human_message(self.thread, 301, reply_to=200)
        second = _human_message(self.thread, 302, reply_to=301)
        self.thread.messages[301] = first
        self.thread.messages[302] = second
        await self.runtime.bot.on_message(first)
        await self.runtime.bot.on_message(first)
        await self.runtime.bot.on_message(second)
        objects, mappings = self._snapshot()
        self.assertEqual(len(objects), 3)
        self.assertEqual(len(mappings), 3)
        by_source = {row[5]: row for row in objects}
        self.assertEqual(set(by_source), {'200', '301', '302'})
        self.assertEqual(by_source['301'][2], by_source['200'][1])
        self.assertEqual(by_source['302'][2], by_source['301'][1])
        self.assertEqual({row[4] for row in objects}, {'200'})
        self.assertEqual(self.runtime.fedify_gateway.render_community.await_count, 3)
        await self.runtime.close()
        from src.app import build_runtime
        self.runtime = build_runtime(self.settings)
        self._attach_external_boundaries(self.runtime)
        for event in (self.thread.starter_message, first, second):
            if event is self.thread.starter_message:
                await self.runtime.bot.on_thread_create(self.thread)
            else:
                await self.runtime.bot.on_message(event)
        self.assertEqual(self._snapshot(), (objects, mappings))
        self.runtime.fedify_gateway.render_community.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
