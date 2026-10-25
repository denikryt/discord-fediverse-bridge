"""Inbound AP Create retains authoritative content before any physical receipt."""
from __future__ import annotations

from support.fake_platform_adapter import FakeDiscordPort

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
import tempfile
import unittest
from unittest.mock import patch

from src.activitypub_models import ActivityPubEvent
from src.community_sync.runtime import CommunityRuntime
from src.db import Database
from src.platform_types import CreatedThread, MessageRef, ThreadRef


class InboundRuntimeSnapshotScenarios(unittest.IsolatedAsyncioTestCase):
    """Check real runtime + SQLite while replacing only the Discord network edge."""

    async def asyncSetUp(self):
        """Create one accepted remote community forum and its v1 schema."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = Database(f'sqlite:///{Path(temp.name) / "runtime.sqlite"}')
        self.addCleanup(self.db.close)
        self.db.migrate()
        self.actor = 'https://remote.test/c/demo'
        self.db.remote_subscriptions.create_subscription(
            discord_channel_id=101, discord_guild_id=1,
            lemmy_community_actor_id=self.actor, lemmy_community_name='demo',
            lemmy_community_id=1, community_inbox_url=f'{self.actor}/inbox',
            follow_activity_id='https://bridge.test/follow/101', status='accepted',
        )
        self.runtime = CommunityRuntime(database=self.db, content_publish_service=object())

    def event(self, *, body='Original body') -> ActivityPubEvent:
        """Build a validated inbound post with a stable, author-owned AP ID."""
        timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
        return ActivityPubEvent.model_validate({
            'event_type': 'post.created', 'delivery_id': 'https://remote.test/a/create/7',
            'occurred_at': timestamp, 'community_actor_id': self.actor,
            'actor_id': 'https://remote.test/u/bob',
            'object': {'ap_id': 'https://remote.test/o/7', 'kind': 'post',
                       'lemmy_id': 7, 'title': 'Example', 'body_markdown': body,
                       'url': 'https://remote.test/post/7', 'published_at': timestamp,
                       'author_name': 'Bob'},
        })

    async def deliver(self, event):
        """Execute the real runtime with a fake typed transport and result class."""
        class Transport(FakeDiscordPort):
            """Fake only the real Discord POST response."""
            def __init__(self):
                """Count physical create attempts."""
                self.calls = 0

            async def create_forum_thread(self, forum, content, *, title, presentation, attempt):
                """Return one exact target-scoped physical thread+starter receipt."""
                self.calls += 1
                thread = ThreadRef(forum, '501')
                return CreatedThread(thread, MessageRef('discord-primary', '501', '502'), 'bot')

        module = ModuleType('src.activitypub_handlers')

        @dataclass
        class HandlerResult:
            """Expose the same result fields as the SDK-dependent handler module."""
            status: str
            outcome: object
            detail: str

        module.HandlerResult = HandlerResult
        transport = Transport()
        with patch.dict('sys.modules', {'src.activitypub_handlers': module}):
            result = await self.runtime.handle_normalized_inbound_post(event, adapter=transport)
        return result, transport

    async def test_normalized_post_commits_source_snapshot_and_receipt(self):
        """Receipt and content survive subsequent runtime invocations."""
        result, transport = await self.deliver(self.event())
        self.assertEqual(result.status, 'processed')
        self.assertEqual(transport.calls, 1)
        group = self.db.discord_fanout_groups.get_thread_group_by_ap_object('https://remote.test/o/7')
        import json
        snapshot = json.loads(group.current_content_json)
        self.assertEqual((snapshot['bodyMarkdown'], snapshot['authorActorId']),
                         ('Original body', 'https://remote.test/u/bob'))
        again, second = await self.deliver(self.event())
        self.assertEqual(again.status, 'skipped')
        self.assertEqual(second.calls, 0)

    async def test_conflicting_ap_replay_never_overwrites_canonical_body(self):
        """Another body with the same AP object ID cannot replace the first Create."""
        await self.deliver(self.event())
        result, transport = await self.deliver(self.event(body='Forged replacement'))
        self.assertEqual(result.status, 'skipped')
        self.assertEqual(transport.calls, 0)
        group = self.db.discord_fanout_groups.get_thread_group_by_ap_object('https://remote.test/o/7')
        import json
        self.assertEqual(json.loads(group.current_content_json)['bodyMarkdown'], 'Original body')


if __name__ == '__main__':
    unittest.main()
