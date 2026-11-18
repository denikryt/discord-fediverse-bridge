"""Discord root presentation and history identity behavior for Plan 145.

Real renderer and webhook history matcher are exercised without modifying
Discord-origin messages or invoking a network client.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from src.remote_person_webhooks import remote_root_content, RemotePersonWebhookTransport
from src.formatting import feed_comment_content

ACTOR = 'https://remote.example/users/bob'
NOTE = 'https://remote.example/notes/42'


def post(*, body: str = 'A public post', object_id: str = NOTE,
         warning: str = '', attachments: list | None = None):
    """Provide the exact persistent root fields consumed at the webhook edge."""
    note = {'type': 'Note', 'id': object_id, 'summary': warning,
            'attachment': [] if attachments is None else attachments}
    return SimpleNamespace(remote_actor_id=ACTOR, remote_handle='bob',
                           remote_avatar_url=None, content_markdown=body,
                           object_id=object_id, source_activity_json=json.dumps({'object': note}))


class FeedRootRenderingContracts(unittest.TestCase):
    """Enforce fixed attribution/footer, length and link atomicity."""

    def test_header_and_source_footer_survive_root_reedits(self) -> None:
        """Create and Edit must rebuild the same immutable attribution/footer."""
        root = post(body='First', warning='Spoiler')
        first = remote_root_content(root)
        edited = remote_root_content(post(body='Second', warning='Spoiler'))
        self.assertTrue(first.startswith('`bob@remote.example`\n\n'))
        self.assertTrue(edited.startswith('`bob@remote.example`\n\n'))
        self.assertEqual(first.splitlines()[-1], NOTE)
        self.assertEqual(edited.splitlines()[-1], NOTE)
        self.assertEqual(edited.count(NOTE), 1)
        self.assertIn('Content warning: Spoiler', edited)
        self.assertNotIn('First', edited)

    def test_limits_and_all_link_types_remain_complete(self) -> None:
        """Truncation cannot cut a raw, autolink or Markdown URL in half."""
        links = (
            'https://ref.example/very-long-reference-path',
            '<https://ref.example/second-long-reference>',
            '[two words](https://ref.example/third-path)',
            '[some label](<https://ref.example/fourth-path>)',
        )
        for length in (1999, 2000, 2001, 2600):
            for link in links:
                with self.subTest(length=length, link=link):
                    rendered = remote_root_content(post(body='x' * (length - 30) + ' ' + link + ' tail'))
                    self.assertLessEqual(len(rendered), 2000)
                    self.assertEqual(rendered.splitlines()[-1], NOTE)
                    # Either the entire Markdown/link fragment is present or
                    # none of it may be left as an unusable prefix.
                    self.assertNotIn(link[:len(link)//2], rendered) if link not in rendered else None
        self.assertEqual(remote_root_content(post(body='')) .splitlines()[-1], NOTE)

    def test_media_links_and_unicode_are_atomic(self) -> None:
        """Signed attachments and Unicode share the body budget with CW."""
        media = 'https://media.example/attachment/large-photo.jpeg'
        rendered = remote_root_content(post(body='🦊' * 2200,
            attachments=[{'url': media}], warning='⚠️ Sensitive'))
        self.assertLessEqual(len(rendered.encode('utf-16-le')) // 2, 2000)
        self.assertEqual(rendered.splitlines()[-1], NOTE)
        self.assertTrue(media in rendered or 'https://media.example/attachment/' not in rendered)

    def test_bad_or_overlong_canonical_identity_rejected(self) -> None:
        """Never send a partial or multiline reconciliation key."""
        for url in ('https://evil.example/notes/42\nrogue',
                    'https://evil.example/notes/<42>',
                    'https://remote.example/notes/' + 'x' * 2300):
            with self.subTest(url=url[:80]), self.assertRaises(ValueError):
                remote_root_content(post(object_id=url))

    def test_display_name_containing_at_sign_is_not_split(self) -> None:
        """A Person display name is not a canonical handle separator."""
        root = post()
        root.remote_handle = 'Alice @ Work'
        rendered = remote_root_content(root)
        self.assertTrue(rendered.startswith('`Alice @ Work@remote.example`\n\n'))

    def test_whitespace_cut_must_not_split_markdown_atom(self) -> None:
        """A late whitespace adjustment cannot cut a protected Markdown label."""
        link = '[read this multi word label](https://ref.example/a)'
        for n in range(1850, 2000):
            with self.subTest(prefix=n):
                rendered = remote_root_content(post(body='x' * n + ' ' + link + ' ' + 'y' * 300))
                self.assertLessEqual(len(rendered.encode('utf-16-le')) // 2, 2000)
                self.assertTrue(link in rendered or '[read' not in rendered)


    def test_nested_url_parentheses_and_escaped_label_bracket_remain_atomic(self) -> None:
        """A cutoff inside a balanced URL or escaped label cannot emit partial Markdown."""
        links = ('[read this multi word label](https://ref.example/a(b)c)',
                 r'[see \] this label](https://ref.example/a)')
        for link in links:
            for n in range(1850, 1980):
                with self.subTest(link=link, prefix=n):
                    rendered = remote_root_content(post(body='x' * n + ' ' + link + ' ' + 'y' * 300))
                    self.assertLessEqual(len(rendered.encode('utf-16-le')) // 2, 2000)
                    self.assertTrue(link in rendered or link.split('](', 1)[0][:4] not in rendered)

    def test_comment_renderer_preserves_balanced_and_escaped_markdown_links(self) -> None:
        """Bot copy headers and masked identities reserve complete body links."""
        links = ('[read this multi word label](https://ref.example/a(b)c)',
                 r'[see \] this label](https://ref.example/a)')
        for link in links:
            for prefix in range(1840, 1950):
                with self.subTest(link=link, prefix=prefix):
                    content = feed_comment_content(
                        actor_name='bob', actor_id=ACTOR, object_id=NOTE,
                        body='x' * prefix + ' ' + link + ' ' + 'y' * 300,
                    )
                    self.assertLessEqual(len(content.encode('utf-16-le')) // 2, 2000)
                    self.assertTrue(content.startswith(f'`bob@remote.example` [↗](<{NOTE}>)'))
                    self.assertTrue(link in content or link.split('](', 1)[0][:4] not in content)

    def test_exact_root_history_match_requires_right_webhook_channel_and_footer(self) -> None:
        """Quoting an ID, a prefix URL or a foreign webhook proves nothing."""
        class Channel:
            """Expose production _channel contract and async Discord history."""
            type = 0
            guild = SimpleNamespace(me=object())
            def __init__(self):
                self.messages = []
            def permissions_for(self, user):
                return SimpleNamespace(**{name: True for name in (
                    'view_channel', 'read_message_history', 'send_messages',
                    'manage_webhooks', 'create_public_threads', 'send_messages_in_threads')})
            async def history(self, *, limit: int):
                """Yield the concrete Discord history rows without a real API."""
                for item in self.messages[:limit]:
                    yield item
        channel = Channel()
        bot = SimpleNamespace(get_channel=lambda id: channel)
        transport = RemotePersonWebhookTransport(bot)
        foreign = SimpleNamespace(id=1, webhook_id=888, content=f'{NOTE}\n\nOther body')
        quote = SimpleNamespace(id=2, webhook_id=123, content=f'Quoted {NOTE}\n\nhttps://remote.example/notes/420')
        wrong = SimpleNamespace(id=3, webhook_id=888, content=f'x\n\n{NOTE}')
        real = SimpleNamespace(id=4, webhook_id=123, content=remote_root_content(post()))
        channel.messages = [foreign, quote, wrong]
        self.assertIsNone(asyncio.run(transport.find_created_root(100, 123, NOTE)))
        channel.messages.append(real)
        self.assertEqual(asyncio.run(transport.find_created_root(100, 123, NOTE)), 4)
        channel.messages.append(SimpleNamespace(id=5, webhook_id=123, content=real.content))
        self.assertIsNone(asyncio.run(transport.find_created_root(100, 123, NOTE)))


if __name__ == '__main__':
    unittest.main()
