"""Gateway callback regressions for connector-scoped published Discord sources.

These scenarios use a real migrated SQLite database and the production
BridgeBot callback entry points. Only the Discord SDK observation/ingress edge
is simulated; lookup ownership remains in the production repository.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.db import Database
from src.discord_bot import BridgeBot
from src.models import PublishedActivityObject
from src.platform_types import MessageRef
from src.personal_feed_conversations import PersonalFeedConversationRuntime


class _Thread:
    """Represent one concrete SDK thread with an observed parent forum."""

    def __init__(self, *, thread_id: int = 600, parent_id: int = 300) -> None:
        """Keep the physical thread and its immediate parent distinct."""
        self.id = thread_id
        self.parent_id = parent_id
        self.owner_id = 20


class _IngressAdapter:
    """Capture the outer normalized SDK ingress boundary without publishing."""

    def __init__(self) -> None:
        """Record admitted operations and balanced callback lifetimes."""
        self.events: list[tuple[str, int]] = []
        self.open_callbacks = 0

    def enter_ingress(self) -> bool:
        """Admit one real BridgeBot event callback."""
        self.open_callbacks += 1
        return True

    def leave_ingress(self) -> None:
        """Observe that the callback joined before returning."""
        self.open_callbacks -= 1

    async def ingest_thread_created(self, thread: object) -> None:
        """Record only the intended physical thread admission."""
        self.events.append(('thread', thread.id))

    async def ingest_thread_message_created(self, message: object) -> None:
        """Record only the intended physical message admission."""
        self.events.append(('message', message.id))


class _Router:
    """Admit a single subscribed forum without creating a federation fake."""

    def is_personal_feed_thread(self, _thread_id: int) -> bool:
        """No discussion thread has been adopted as a Personal Feed."""
        return False

    def is_local_community_forum(self, forum_id: int) -> bool:
        """Only the selected community forum may publish."""
        return forum_id == 300

    def is_remote_subscription_forum(self, _forum_id: int) -> bool:
        """Exclude remote subscriptions from this local-source regression."""
        return False


class DiscordExactSourceGatewayScenarios(unittest.IsolatedAsyncioTestCase):
    """Prove that scoped community sources cannot break SDK callback routing."""

    def setUp(self) -> None:
        """Migrate real SQLite and persist a typed canonical community root."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sqlite_url = f"sqlite:///{Path(self.temp.name) / 'gateway.db'}"
        self.database = Database(self.sqlite_url)
        self.addCleanup(self.database.close)
        self.database.migrate()
        # The starter has an exact source (thread container + message ID).
        # Legacy ID-only lookup must not be used even when the ID is unique.
        with self.database.session() as session:
            session.add(PublishedActivityObject(
                source_connector_id=1,
                source_external_container_id='600',
                source_external_message_id='600',
                actor_username='alice',
                actor_url='https://bridge.test/actors/users/alice',
                publication_context='community',
                community_actor_url='https://bridge.test/actors/communities/demo',
                activity_id='https://bridge.test/activities/root',
                object_id='https://bridge.test/objects/root',
                kind='post',
                body_markdown='first forum post',
                discord_channel_id=300,
                discord_message_id=600,
            ))
            # The same numeric ID under a different immediate container must
            # not misclassify the community forum as a Personal Feed thread.
            session.add(PublishedActivityObject(
                source_connector_id=1,
                source_external_container_id='950',
                source_external_message_id='600',
                actor_username='bob',
                actor_url='https://bridge.test/actors/users/bob',
                publication_context='personal_feed',
                conversation_root_object_id='https://bridge.test/objects/unrelated-feed',
                activity_id='https://bridge.test/activities/unrelated-feed',
                object_id='https://bridge.test/objects/unrelated-feed',
                kind='post', body_markdown='unrelated note',
                discord_guild_id=1, discord_channel_id=950,
                discord_message_id=600,
            ))
        self.adapter = _IngressAdapter()
        self.bot = object.__new__(BridgeBot)
        self.bot._connection = SimpleNamespace(user=SimpleNamespace(id=999))
        self.bot.database = self.database
        self.bot.discord_adapter = self.adapter
        self.bot.event_router = _Router()
        self.bot._thread_locks = {}

    async def test_personal_feed_discussion_adopts_its_exact_scoped_root(self) -> None:
        """The next runtime boundary also resolves personal roots by parent channel."""
        with self.database.session() as session:
            session.add(PublishedActivityObject(
                source_connector_id=1,
                source_external_container_id='950',
                source_external_message_id='850',
                actor_username='bob',
                actor_url='https://bridge.test/actors/users/bob',
                publication_context='personal_feed',
                conversation_root_object_id='https://bridge.test/objects/feed',
                activity_id='https://bridge.test/activities/feed',
                object_id='https://bridge.test/objects/feed',
                kind='post',
                body_markdown='feed root',
                discord_guild_id=1,
                discord_channel_id=950,
                discord_message_id=850,
            ))
        runtime = PersonalFeedConversationRuntime(
            database=self.database, personal_feed_service=SimpleNamespace(),
            fedify_gateway=SimpleNamespace(), user_ban_service=SimpleNamespace(),
            bridge_policy_service=SimpleNamespace(),
            fedify_origin='https://bridge.test',
        )
        thread = _Thread(thread_id=850, parent_id=950)
        self.bot.event_router = SimpleNamespace(
            is_local_community_forum=lambda _id: False,
            is_remote_subscription_forum=lambda _id: False,
            is_personal_feed_thread=lambda _id: False,
        )
        outcomes = []

        class _PersonalIngress(_IngressAdapter):
            """Join SDK admission to the real Personal Feed adoption runtime."""

            async def ingest_thread_created(self, observed: object) -> None:
                """Apply the checked physical root to the SQLite conversation."""
                outcomes.append(runtime.adopt_manual_thread(thread=observed))

        self.adapter = _PersonalIngress()
        self.bot.discord_adapter = self.adapter
        await BridgeBot.on_thread_create(self.bot, thread)
        await BridgeBot.on_thread_create(self.bot, thread)
        self.assertEqual([result.status for result in outcomes], ['adopted', 'adopted'])
        self.assertEqual(outcomes[0].object_id, 'https://bridge.test/objects/feed')
        stored = self.database.personal_feed_conversations.get_thread_by_thread_id(850)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.root_discord_channel_id, 950)
        wrong = runtime.adopt_manual_thread(thread=SimpleNamespace(id=850, parent_id=951))
        self.assertEqual(wrong.status, 'ignored')
        self.assertIsNone(self.database.personal_feed_conversations.get_thread_by_thread_id(600))

    async def test_legacy_personal_feed_root_remains_adoptable_by_exact_channel(self) -> None:
        """A pre-scoped Personal Feed root survives without ambiguous ID lookup."""
        with self.database.session() as session:
            session.add(PublishedActivityObject(
                source_connector_id=1,
                source_external_container_id=None,
                source_external_message_id='851',
                actor_username='bob',
                actor_url='https://bridge.test/actors/users/bob',
                publication_context='personal_feed',
                conversation_root_object_id='https://bridge.test/objects/old-feed',
                activity_id='https://bridge.test/activities/old-feed',
                object_id='https://bridge.test/objects/old-feed',
                kind='post', body_markdown='historical root',
                discord_guild_id=1, discord_channel_id=950,
                discord_message_id=851,
            ))
        runtime = PersonalFeedConversationRuntime(
            database=self.database, personal_feed_service=SimpleNamespace(),
            fedify_gateway=SimpleNamespace(), user_ban_service=SimpleNamespace(),
            bridge_policy_service=SimpleNamespace(),
            fedify_origin='https://bridge.test',
        )
        self.bot.event_router = SimpleNamespace(
            is_local_community_forum=lambda _id: False,
            is_remote_subscription_forum=lambda _id: False,
        )
        admitted = []

        class _HistoricalIngress(_IngressAdapter):
            """Drive the existing conversation service from the SDK edge."""

            async def ingest_thread_created(self, thread: object) -> None:
                """Apply the verified legacy source through durable adoption."""
                admitted.append(runtime.adopt_manual_thread(thread=thread).status)

        self.bot.discord_adapter = _HistoricalIngress()
        await BridgeBot.on_thread_create(self.bot, _Thread(thread_id=851, parent_id=950))
        self.assertEqual(admitted, ['adopted'])
        self.assertIsNotNone(self.database.personal_feed_conversations.get_thread_by_thread_id(851))
        wrong = runtime.adopt_manual_thread(
            thread=SimpleNamespace(id=851, parent_id=951)
        )
        self.assertEqual(wrong.status, 'ignored')
        self.assertIsNone(self.database.personal_feed_conversations.get_thread_by_thread_id(600))

    async def test_replayed_community_thread_uses_exact_source_without_exception(self) -> None:
        """Two Gateway THREAD_CREATE notices never use the ID-only AP lookup."""
        thread = _Thread()
        await BridgeBot.on_thread_create(self.bot, thread)
        await BridgeBot.on_thread_create(self.bot, thread)
        # Reopen the original physical source through a separate SQLite owner:
        # replay after restart must not trigger the old ID-only lookup either.
        restarted = Database(self.sqlite_url)
        self.addCleanup(restarted.close)
        restarted.migrate()
        self.bot.database = restarted
        await BridgeBot.on_thread_create(self.bot, thread)
        # Dedup belongs to the durable publisher, not this SDK admission gate.
        # Replays reach the sink without altering this persisted source row.
        self.assertEqual(self.adapter.events, [('thread', 600)] * 3)
        self.assertEqual(self.adapter.open_callbacks, 0)
        stored = restarted.activitypub_objects.get_by_source_ref(
            MessageRef('discord-primary', '600', '600'),
        )
        self.assertEqual(stored.object_id, 'https://bridge.test/objects/root')

    async def test_community_reply_does_not_adopt_an_unrelated_personal_root(self) -> None:
        """A normal forum reply is routed even when a typed source root exists."""
        message = SimpleNamespace(
            id=700, channel=_Thread(),
            guild=SimpleNamespace(id=1),
            author=SimpleNamespace(id=21, bot=False),
        )
        with patch('src.discord_bot.discord.Thread', _Thread):
            await BridgeBot.on_message(self.bot, message)
            await BridgeBot.on_message(self.bot, message)
        self.assertEqual(self.adapter.events, [('message', 700), ('message', 700)])
        self.assertEqual(self.adapter.open_callbacks, 0)
        # In particular, the root was not interpreted as a Personal Feed Note.
        self.assertIsNone(self.database.activitypub_objects.get_by_source_ref(
            MessageRef('discord-primary', '300', '600'),
        ))
