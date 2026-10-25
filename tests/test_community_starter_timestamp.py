"""Standalone timestamp/provenance regressions for THREAD_CREATE-only publication."""
from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.discord_adapter import DiscordAdapter
from src.platform_types import ConnectorRef, ContentDocument, MessageRef, MessageView, PlatformOperationError


class StarterTimestampTests(unittest.TestCase):
    """Publication cannot replace physical creation time with an ingress time."""

    def test_message_view_accepts_aware_timestamp_without_replacing_instant(self) -> None:
        """An observed offset remains the same source instant across layers."""
        observed=datetime(2026,10,23,13,15,tzinfo=timezone(timedelta(hours=3)))
        view=MessageView(MessageRef('discord-primary','200','300'),created_at=observed)
        self.assertEqual(view.created_at.astimezone(timezone.utc).hour,10)

    def test_message_view_rejects_naive_source_creation(self) -> None:
        """Naive or malformed SDK datetimes can never become AP published_at."""
        with self.assertRaises(ValueError):
            MessageView(MessageRef('discord-primary','200','300'),created_at=datetime(2026,10,23))

    def test_adapter_fetched_starter_preserves_actual_creation_time(self) -> None:
        """A cold-cache THREAD_CREATE fetch supplies creation distinct from edit."""
        async def exercise():
            adapter=DiscordAdapter(connector=ConnectorRef('discord-primary','discord'),bot=SimpleNamespace(user=None))
            created=datetime(2026,10,23,6,0,tzinfo=timezone.utc)
            edited=datetime(2026,10,23,7,0,tzinfo=timezone.utc)
            ref=MessageRef('discord-primary','200','300')
            physical=SimpleNamespace(id=300,channel=SimpleNamespace(id=200),content='Post body',attachments=[],created_at=created,edited_at=edited,
                                     author=SimpleNamespace(id=123,bot=False,display_name='Alice'),webhook_id=None)
            async def read(_ref):
                self.assertEqual(_ref,ref)
                return physical
            adapter._get_message=read
            result=await adapter.fetch_message(ref)
            self.assertEqual(result.created_at,created)
            self.assertEqual(result.edited_at,edited)
            self.assertEqual(result.content,ContentDocument('Post body'))
            return adapter
        asyncio.run(exercise())

    def test_adapter_mismatched_physical_container_fails_closed(self) -> None:
        """Only the exact fetched thread/message pair may authorize publication."""
        async def exercise():
            adapter=DiscordAdapter(connector=ConnectorRef('discord-primary','discord'),bot=SimpleNamespace(user=None))
            physical=SimpleNamespace(id=300,channel=SimpleNamespace(id=999),content='Fake',attachments=[],created_at=datetime.now(timezone.utc),edited_at=None,author=None)
            async def read(_ref): return physical
            adapter._get_message=read
            with self.assertRaises(PlatformOperationError):
                await adapter.fetch_message(MessageRef('discord-primary','200','300'))
        asyncio.run(exercise())

    def test_adapter_missing_timestamp_fails_closed(self) -> None:
        """A Discord response without source time is not a canonical starter."""
        async def exercise():
            adapter=DiscordAdapter(connector=ConnectorRef('discord-primary','discord'),bot=SimpleNamespace(user=None))
            physical=SimpleNamespace(id=300,channel=SimpleNamespace(id=200),content='Post',attachments=[],edited_at=None,author=None)
            async def read(_ref): return physical
            adapter._get_message=read
            with self.assertRaises(PlatformOperationError):
                await adapter.fetch_message(MessageRef('discord-primary','200','300'))
        asyncio.run(exercise())


if __name__=='__main__':
    unittest.main(verbosity=2)
