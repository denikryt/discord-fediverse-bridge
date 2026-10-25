"""Typed Discord boundary fake shared by durable community fanout scenarios.

It deliberately implements the full lookup/write port, allowing the real
connector registry to reject mismatched or unconfigured adapters in tests.
Only a test's explicitly overridden network actions may produce receipts.
"""
from __future__ import annotations

from src.platform_types import ConnectorRef


class FakeDiscordPort:
    """Supply strict, non-networking defaults for one Discord-primary adapter."""

    connector = ConnectorRef('discord-primary', 'discord', None)

    async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
        """Require a scenario to implement its own signed physical Create."""
        raise AssertionError('unexpected physical forum creation')

    async def create_reply_thread(self, root, *, title, attempt):
        """Reject unsupported physical reply-thread creation."""
        raise AssertionError('unexpected reply thread creation')

    async def send_message(self, destination, content, *, reply_to, presentation, attempt):
        """Require a scenario to implement the exact physical message result."""
        raise AssertionError('unexpected physical message creation')

    async def edit_message(self, message, content):
        """Reject unrelated physical mutation during Create scenarios."""
        raise AssertionError('unexpected edit')

    async def delete_message(self, message):
        """Reject unrelated physical mutation during Create scenarios."""
        raise AssertionError('unexpected delete')

    async def fetch_channel(self, channel):
        """Reject any live lookup that was not specified by the scenario."""
        raise AssertionError('unexpected channel fetch')

    async def fetch_thread(self, thread):
        """Reject any live lookup that was not specified by the scenario."""
        raise AssertionError('unexpected thread fetch')

    async def fetch_message(self, message):
        """Reject any live lookup that was not specified by the scenario."""
        raise AssertionError('unexpected message fetch')

    async def reconcile_create(self, attempt):
        """Never manufacture same-attempt proof for a lost Discord response."""
        return None

    async def check_permissions(self, principal, channel, actions):
        """Reject implicit permissions from a transport fake."""
        raise AssertionError('unexpected permission lookup')

    async def capabilities(self, channel):
        """Reject assumed capabilities in scenarios without provider metadata."""
        raise AssertionError('unexpected capabilities lookup')
