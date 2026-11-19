"""Plan 146 fit-first feed layout regressions; Person commands follow in CP44."""
from __future__ import annotations

import unittest

from src.formatting import (DISCORD_MESSAGE_LIMIT, feed_comment_content,
                            render_feed_discord_message)


OBJECT = 'https://peer.example/notes/42'
ACTOR = 'https://peer.example/users/bob'


def _units(message: str) -> int:
    """Assert the same UTF-16 limit Discord applies to sent content."""
    return len(message.encode('utf-16-le')) // 2


class FeedFitFirstLayoutTests(unittest.TestCase):
    """A fitting source URL is kept whole even after the original body cutoff."""

    def _renderers(self):
        """Root footer and comment marker use the same optional body budget."""
        return (
            lambda body: render_feed_discord_message(header='', body=body, footer=OBJECT),
            lambda body: feed_comment_content(actor_name='bob', actor_id=ACTOR,
                                              object_id=OBJECT, body=body),
        )

    def test_crossing_url_is_retained_by_middle_omission(self) -> None:
        """Unimportant prefix text yields space to one complete late link."""
        formats = (
            'https://reference.example/a(b)c',
            '<https://reference.example/d>',
            '[full multi word label](https://reference.example/a(b)c)',
            r'[see \] this label](https://reference.example/e)',
            '[description](<https://reference.example/f>)',
        )
        for link in formats:
            for prefix in (1900, 1970, 2020):
                for render in self._renderers():
                    with self.subTest(link=link, prefix=prefix, renderer=render):
                        result = render('x' * prefix + ' ' + link + ' tail')
                        self.assertLessEqual(_units(result), DISCORD_MESSAGE_LIMIT)
                        self.assertIn(link, result)
                        if prefix >= 1970:
                            self.assertIn('…', result)
                        self.assertEqual(result.count(link), 1)
                        self.assertTrue(result.endswith(OBJECT) or
                                        result.startswith(f'`bob@peer.example` [↗](<{OBJECT}>)'))

    def test_multiple_links_preserve_relative_order(self) -> None:
        """Preserve early complete links and their order without URL shortening."""
        links = ['https://reference.example/first',
                 '[read more](https://reference.example/second)',
                 'https://reference.example/third']
        body = 'a' * 1900 + ' ' + ' words '.join(links) + ' z' * 120
        for render in self._renderers():
            text = render(body)
            self.assertLessEqual(_units(text), DISCORD_MESSAGE_LIMIT)
            positions = [text.index(link) for link in links]
            self.assertEqual(positions, sorted(positions))
            self.assertEqual(text.count('https://reference.example/'), 3)

    def test_unfit_atom_is_dropped_not_fragmented(self) -> None:
        """A URL larger than optional capacity never becomes a damaged prefix."""
        huge = 'https://reference.example/' + 'z' * 2050
        for render in self._renderers():
            result = render('hello ' + huge + ' after')
            self.assertLessEqual(_units(result), DISCORD_MESSAGE_LIMIT)
            self.assertNotIn('https://reference.example/', result)
            self.assertIn('hello', result)
            self.assertIn('…', result)

    def test_empty_unicode_limits_and_mandatory_metadata(self) -> None:
        """Boundary lengths include surrogate pairs, header and footer."""
        for render in self._renderers():
            empty = render('')
            self.assertIn(OBJECT, empty)
            for size in (1999, 2000, 2001, 3000):
                result = render('🦊' * size)
                self.assertLessEqual(_units(result), DISCORD_MESSAGE_LIMIT)
                self.assertIn(OBJECT, result)
        with self.assertRaises(ValueError):
            render_feed_discord_message(header='', body='content')
        with self.assertRaises(ValueError):
            render_feed_discord_message(header='', body='content', footer='https://example.org/' + 'a' * 3000)


if __name__ == '__main__':
    unittest.main()
