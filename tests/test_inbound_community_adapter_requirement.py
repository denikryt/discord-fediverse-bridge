"""Create paths refuse unbound adapters instead of falling back to raw SDK sends."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.community_sync.runtime import CommunityRuntime


class NoSdkApFallbackScenarios(unittest.IsolatedAsyncioTestCase):
    """Reject an AP Create before any untyped network or repository access."""

    async def test_remote_post_requires_a_bound_adapter(self) -> None:
        """An unbound AP post cannot bypass durable intent reservation."""
        with self.assertRaisesRegex(RuntimeError, 'bound platform adapter'):
            await CommunityRuntime.handle_inbound_post(
                object(), object(), SimpleNamespace(discord_adapter=None)
            )

    async def test_remote_comment_requires_a_bound_adapter(self) -> None:
        """An unbound AP comment cannot use an old Discord send path."""
        with self.assertRaisesRegex(RuntimeError, 'bound platform adapter'):
            await CommunityRuntime.handle_inbound_comment(
                object(), object(), SimpleNamespace(discord_adapter=None)
            )
