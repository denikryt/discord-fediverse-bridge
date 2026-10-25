"""Real typed boundary tests for source timestamps with no Discord Gateway callback."""
from __future__ import annotations
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.content_publish_service import ContentPublishService, PublishResult
from src.platform_types import (
    AuthorRef, ChannelRef, ContentDocument, MessageCreated, MessageRef,
    MessageView, PlatformOperationError, ThreadCreated, ThreadRef,
)

class NormalizedPublicationTimestampCases(unittest.TestCase):
    """Canonical publication timestamps are obtained from the physical source."""

    def setUp(self) -> None:
        self.service=object.__new__(ContentPublishService)
        self.service._resolve_normalized_author=AsyncMock(return_value=(SimpleNamespace(activitypub_username='alice',actor_url='https://bridge.test/u/alice'),None))
        self.service._publish_post_fields=AsyncMock(return_value=PublishResult('published','published'))
        self.service._publish_comment_fields=AsyncMock(return_value=PublishResult('published','published'))
        self.forum=ChannelRef('discord-primary','900','forum','1')
        self.thread=ThreadRef(self.forum,'200')
        self.message=MessageRef('discord-primary','200','300')
        self.author=AuthorRef('discord-primary','123','human','Alice')
        self.created=datetime(2026,10,23,13,0,tzinfo=timezone(timedelta(hours=3)))

    def test_thread_create_without_message_create_preserves_source_created_at(self):
        """A standalone fetched THREAD_CREATE never uses event.received_at."""
        async def invoke():
            event=ThreadCreated('discord-primary',self.thread,'A post',starter=self.message,
                                external_guild_id='1',received_at=datetime(2026,10,23,15,0,tzinfo=timezone.utc))
            view=MessageView(self.message,self.author,ContentDocument('Text'),
                             edited_at=datetime(2026,10,23,14,0,tzinfo=timezone.utc),
                             created_at=self.created)
            await self.service.publish_normalized_post_to_community(
                event=event,starter=view,community_actor_url='https://remote.test/c/community',
                publish_call=AsyncMock())
        asyncio.run(invoke())
        args=self.service._publish_post_fields.await_args.kwargs
        self.assertEqual(args['published_at'],self.created.astimezone(timezone.utc))
        self.assertEqual(args['source_message'],self.message)

    def test_missing_starter_time_is_rejected_before_author_or_network(self):
        """A starter without physical creation time cannot publish to AP."""
        event=ThreadCreated('discord-primary',self.thread,'A post',starter=self.message,external_guild_id='1')
        view=MessageView(self.message,self.author,ContentDocument('Text'))
        async def invoke():
            with self.assertRaises(PlatformOperationError):
                await self.service.publish_normalized_post_to_community(
                    event=event,starter=view,community_actor_url='https://remote.test/c/community',
                    publish_call=AsyncMock())
        asyncio.run(invoke())
        self.service._publish_post_fields.assert_not_awaited()
        self.service._resolve_normalized_author.assert_not_awaited()

    def test_comment_uses_source_created_at(self):
        """A reply keeps its own message creation instant, not callback time."""
        event=MessageCreated('discord-primary',self.message,self.author,ContentDocument('Reply'),
                            parent_channel=self.forum,thread_context=self.thread,
                            external_guild_id='1',created_at=self.created)
        async def invoke():
            await self.service.publish_normalized_comment_to_community(
                event=event,community_actor_url='https://remote.test/c/community',
                parent_object_id='https://remote.test/o/parent',publish_call=AsyncMock())
        asyncio.run(invoke())
        args=self.service._publish_comment_fields.await_args.kwargs
        self.assertEqual(args['published_at'],self.created.astimezone(timezone.utc))
        self.assertEqual(args['source_message'],self.message)

    def test_missing_comment_time_fails_closed(self):
        """An omitted normalized comment timestamp is not fabricated by ingress."""
        event=MessageCreated('discord-primary',self.message,self.author,ContentDocument('Reply'),
                            parent_channel=self.forum,thread_context=self.thread,external_guild_id='1')
        async def invoke():
            with self.assertRaises(PlatformOperationError):
                await self.service.publish_normalized_comment_to_community(
                    event=event,community_actor_url='https://remote.test/c/community',
                    parent_object_id='https://remote.test/o/parent',publish_call=AsyncMock())
        asyncio.run(invoke())
        self.service._resolve_normalized_author.assert_not_awaited()

if __name__=='__main__': unittest.main(verbosity=2)
