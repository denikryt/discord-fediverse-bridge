"""Discord bot comment layout and ambiguous-send history recovery contracts.

Production renderers and transport are exercised with an in-memory Discord
adapter; message ownership and history boundaries are real transport rules.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timezone
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.remote_person_comment_delivery import BotCommentTransport, CommentDispatch
from src.formatting import feed_comment_content

URL = 'https://remote.example/notes/42'
ACTOR = 'https://other.example/users/alice'


def claim(*, object_id: str = URL, body: str = 'hello', actor_id: str = ACTOR) -> CommentDispatch:
    """Build an authorized immutable delivery snapshot for a known thread."""
    return CommentDispatch(placement_id=1, claim_token='token', root_id=2,
        guild_id=99, channel_id=10, thread_id=20,
        root_actor_id='https://remote.example/users/root', object_id=object_id,
        body=body, actor_name='Alice', actor_id=actor_id,
        parent_message_id=None, revision='v1')


class Thread:
    """Record bot sends and return immutable Discord history row objects."""
    def __init__(self):
        """Create a writable thread owned by the expected guild."""
        self.id = 20
        self.guild = SimpleNamespace(id=99, me=object())
        self.messages = []
        self.sent = []

    def permissions_for(self, member):
        """Grant effective bot thread permissions for the normal case."""
        return SimpleNamespace(view_channel=True, read_message_history=True,
                               send_messages_in_threads=True)

    async def send(self, content, **options):
        """Record submitted content and return a bot-authored response."""
        self.sent.append((content, options))
        return SimpleNamespace(id=200 + len(self.sent))

    async def history(self, *, limit):
        """Yield messages as Discord returns them, newest first."""
        for message in self.messages[:limit]:
            yield message


class CommentRenderingContracts(unittest.TestCase):
    """Protect bot message identity without modifying Discord user sources."""

    def setUp(self):
        """Use an isolated Discord SDK boundary for each scenario."""
        self.thread = Thread()
        self.bot = SimpleNamespace(get_channel=lambda _: self.thread,
            fetch_channel=None, user=SimpleNamespace(id=500))
        self.transport = BotCommentTransport(self.bot)
        discord = types.ModuleType('discord')
        discord.AllowedMentions = SimpleNamespace(none=lambda: object())
        discord.MessageReference = lambda **kw: SimpleNamespace(**kw)
        discord.HTTPException = type('HTTPException', (Exception,), {})
        self.sdk = patch.dict(sys.modules, {'discord': discord})
        self.sdk.start()
        self.addCleanup(self.sdk.stop)

    def message(self, mid: int, text: str, *, sender: int = 500):
        """Represent one actual history entry with a Discord author ID."""
        return SimpleNamespace(id=mid, content=text, author=SimpleNamespace(id=sender))

    def test_send_uses_own_actor_and_exact_first_header_marker(self):
        """Sending one reply preserves author, native reference and marker."""
        nested = claim(body='hello https://ref.example/one', actor_id=ACTOR)
        nested = CommentDispatch(**{**{f: getattr(nested, f) for f in nested.__dataclass_fields__},
                                    'parent_message_id': 900})
        self.assertEqual(asyncio.run(self.transport.send(nested)), 201)
        content, options = self.thread.sent[0]
        self.assertEqual(content.split('\n\n')[0], '`Alice@other.example` [↗](<' + URL + '>)')
        self.assertEqual(content.count('[↗](<'), 1)
        self.assertEqual(options['reference'].message_id, 900)
        self.assertEqual(options['reference'].channel_id, 20)

    def test_body_link_budget_and_edit_identity(self):
        """New content does not lose canonical identity or truncate a link."""
        before = feed_comment_content(actor_name='Alice', actor_id=ACTOR,
                                      object_id=URL, body='first')
        after = feed_comment_content(actor_name='Alice', actor_id=ACTOR,
            object_id=URL, body='x' * 1940 + ' https://ref.example/very-long-url and tail')
        self.assertEqual(before.split('\n\n')[0], after.split('\n\n')[0])
        self.assertLessEqual(len(after.encode('utf-16-le')) // 2, 2000)
        self.assertNotIn('https://ref.example/very-', after)
        self.assertEqual(after.count('[↗](<'), 1)

    def test_reconcile_rejects_body_quotes_prefix_sender_and_nonunique_matches(self):
        """A quoted identifier or foreign sender cannot establish delivery."""
        correct = feed_comment_content(actor_name='Alice', actor_id=ACTOR,
                                       object_id=URL, body='one')
        other = feed_comment_content(actor_name='Alice', actor_id=ACTOR,
            object_id=URL + '0', body='Quote ' + URL)
        self.thread.messages = [self.message(1, other),
            self.message(2, 'old header\n\nBody quote ' + URL),
            self.message(3, correct, sender=600)]
        self.assertIsNone(asyncio.run(self.transport.find_uncertain(claim())))
        self.thread.messages.append(self.message(4, correct))
        self.assertEqual(asyncio.run(self.transport.find_uncertain(claim())), 4)
        self.thread.messages.append(self.message(5, correct))
        self.assertIsNone(asyncio.run(self.transport.find_uncertain(claim())))

    def test_personal_feed_sqlite_create_and_update_use_identical_identity(self):
        """A remote comment in a local Person thread keeps its Actor on Edit."""
        from tests.behavior.test_personal_feed_reply_ancestry import environment, comment
        from src.personal_feed_discord_delivery_worker import PersonalFeedDiscordDeliveryWorker

        with tempfile.TemporaryDirectory() as tmp:
            database, service, _gateway, _runtime = environment(Path(tmp))
            self.addCleanup(database.close)
            comment_id = 'https://remote.example/o/comment-42'
            source = comment(database, obj_id=comment_id)
            thread = self.thread
            thread.id = 100
            # Personal Feed comments are delivered to an existing user-owned
            # Discord thread. Only the bridge-created comment is editable.
            bot = SimpleNamespace(get_channel=lambda _: thread)
            worker = PersonalFeedDiscordDeliveryWorker(
                database=database, bot=bot, personal_feed_service=service,
            )
            claims = database.personal_feed_conversations.claim_due()
            asyncio.run(worker._deliver(next(row for row in claims if row.delivery_id == source.delivery_id)))
            self.assertEqual(len(thread.sent), 1)
            first_text = thread.sent[0][0]
            self.assertEqual(first_text.split('\n\n')[0],
                             '`Bob@remote.example` [↗](<' + comment_id + '>)')
            self.assertEqual(thread.sent[0][0].count('[↗](<'), 1)
            edited = []

            async def edit(*, content: str, **options):
                """Capture in-place Edit against the original bot message."""
                edited.append(content)

            async def fetch_message(mid):
                """Return only the bot-created comment, not user root."""
                self.assertEqual(mid, 201)
                return SimpleNamespace(edit=edit)

            thread.fetch_message = fetch_message
            result = database.personal_feed_conversations.admit_update(
                object_id=comment_id,
                activity_actor_id='https://remote.example/users/bob',
                attributed_to_actor_id='https://remote.example/users/bob',
                content_markdown='Edited https://ref.example/article',
                updated_at=datetime(2026, 11, 18, tzinfo=timezone.utc),
                source_activity={'type': 'Update'},
            )
            self.assertEqual(result.status, 'accepted')
            claims = database.personal_feed_conversations.claim_due()
            asyncio.run(worker._deliver(next(row for row in claims if row.delivery_id == source.delivery_id)))
            self.assertEqual(len(thread.sent), 1)
            self.assertEqual(len(edited), 1)
            self.assertEqual(edited[0].split('\n\n')[0], first_text.split('\n\n')[0])
            self.assertIn('Edited https://ref.example/article', edited[0])

    def test_reconcile_checks_correct_thread_and_guild(self):
        """Cross-guild/channel history cannot prove a POST in this placement."""
        correct = feed_comment_content(actor_name='Alice', actor_id=ACTOR,
                                       object_id=URL, body='x')
        self.thread.messages.append(self.message(1, correct))
        self.thread.guild.id = 123
        with self.assertRaises(PermissionError):
            asyncio.run(self.transport.find_uncertain(claim()))
        self.thread.guild.id = 99
        self.thread.id = 999
        with self.assertRaises(PermissionError):
            asyncio.run(self.transport.find_uncertain(claim()))


if __name__ == '__main__':
    unittest.main()
