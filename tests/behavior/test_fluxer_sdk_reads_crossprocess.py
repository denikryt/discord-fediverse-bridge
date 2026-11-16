"""Real private HTTP boundary from Python FluxerAdapter to SDK-model Node reads."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from src.fluxer_adapter import FluxerAdapter
from src.fluxer_connector_config import FluxerConnectorConfig
from src.fluxer_rpc_client import FluxerRpcClient
from src.platform_types import (
    AuthorRef, ChannelRef, MessageRef, ThreadRef, PlatformOperationError,
    ContentDocument, DeliveryPresentation, CreateAttempt,
)


class FluxerSdkReadCrossProcessScenarios(unittest.IsolatedAsyncioTestCase):
    """Verify transport-scoped read snapshots through two real processes."""

    async def asyncSetUp(self) -> None:
        """Start a Node HTTP process with a deterministic SDK-edge fake."""
        root = Path(__file__).resolve().parents[2]
        self._node = await asyncio.create_subprocess_exec(
            'node', '--experimental-strip-types',
            str(root / 'fluxer-connector/tests/fixture_sdk_reads.mjs'),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._node.stdout is not None
        ready = await asyncio.wait_for(self._node.stdout.readline(), 8)
        if not ready.strip().isdigit():
            stderr = await self._node.stderr.read() if self._node.stderr else b''
            self.fail(f'Node SDK read fixture failed: {stderr[:200]!r}')
        self.rpc = FluxerRpcClient(FluxerConnectorConfig(
            connector_key='fluxer-contract', origin='https://fluxer.example',
            rpc_url=f'http://127.0.0.1:{int(ready)}', transport_secret='test-transport-secret',
        ))
        self.adapter = FluxerAdapter(connector_key='fluxer-contract',
                                     instance_origin='https://fluxer.example', rpc=self.rpc,
                                     bot_actor_id='bot-id')
        self.forum = ChannelRef('fluxer-contract', 'forum-1', 'forum', 'guild-1')

    async def asyncTearDown(self) -> None:
        """Close the verified RPC connection and join the sidecar."""
        await self.rpc.close()
        if self._node.returncode is None:
            self._node.terminate()
        await asyncio.wait_for(self._node.wait(), 8)

    async def test_forum_thread_message_lookup(self) -> None:
        """SDK confirms a forum, its parent thread and the true message container."""
        forum = await self.adapter.fetch_channel(self.forum)
        self.assertEqual(forum.channel, self.forum)
        thread_ref = ThreadRef(self.forum, 'thread-1')
        thread = await self.adapter.fetch_thread(thread_ref)
        self.assertEqual(thread.thread, thread_ref)
        self.assertIsNone(thread.starter)
        message = await self.adapter.fetch_message(
            MessageRef('fluxer-contract', 'thread-1', 'message-1'))
        self.assertEqual(message.content.body, 'hello from SDK')
        self.assertEqual(message.author.external_actor_id, 'user-1')

    async def test_permissions_are_checked_on_current_bot_with_forum_rights(self) -> None:
        """Fresh guild permissions authorize forum create but never a foreign bot."""
        bot = AuthorRef('fluxer-contract', 'bot-id', 'bot', 'Bridge')
        grants = await self.adapter.check_permissions(bot, self.forum, frozenset({
            'view_channel', 'read_history', 'send_message', 'create_forum_post',
        }))
        self.assertEqual(grants['view_channel'], 'allowed')
        self.assertEqual(grants['read_history'], 'allowed')
        self.assertEqual(grants['send_message'], 'allowed')
        self.assertEqual(grants['create_forum_post'], 'allowed')
        foreign = AuthorRef('fluxer-contract', 'another-bot', 'bot', 'Other')
        denied = await self.adapter.check_permissions(
            foreign, self.forum, frozenset({'view_channel'}))
        self.assertEqual(denied['view_channel'], 'unknown')

    async def test_verified_forum_capability_still_refuses_missing_required_tag(self) -> None:
        """Verified forum support does not bypass the required-tag admission guard."""
        response = await self.rpc.call('capabilities', {'channel_id': 'forum-1'},
                                       connector_key='fluxer-contract')
        self.assertEqual(response['guild_id'], 'guild-1')
        self.assertTrue(response['tag_requirements_verified'])
        self.assertTrue(response['requires_tag'])
        self.assertEqual(response['available_tags'], [
            {'id': 'tag-a', 'moderated': False},
            {'id': 'tag-mod', 'moderated': True},
        ])
        caps = await self.adapter.capabilities(self.forum)
        self.assertTrue(caps.forum_posts)
        self.assertFalse(caps.message_edit_delete)
        self.assertFalse(caps.webhook_send)
        # Server tag policy demands an explicit configured unmoderated tag;
        # advertising a method does not authorize an untagged physical POST.
        with self.assertRaises(PlatformOperationError) as denied:
            await self.adapter.create_forum_thread(
                self.forum, ContentDocument('hello'), title='Title',
                presentation=DeliveryPresentation('bot'),
                attempt=CreateAttempt('probe', 'fluxer-contract', 'forum_thread', self.forum),
            )
        self.assertEqual(denied.exception.reason, 'unsupported')

    async def test_wrong_physical_scope_cannot_be_repaired_by_sdk(self) -> None:
        """A wrong parent, container or instance is rejected without side effects."""
        with self.assertRaises(PlatformOperationError):
            await self.adapter.fetch_thread(ThreadRef(
                ChannelRef('fluxer-contract', 'other-forum', 'forum', 'guild-1'),
                'thread-1'))
        with self.assertRaises(PlatformOperationError):
            await self.adapter.fetch_message(
                MessageRef('fluxer-contract', 'forum-1', 'message-1'))
        with self.assertRaises(PlatformOperationError):
            await self.adapter.fetch_channel(
                ChannelRef('foreign', 'forum-1', 'forum', 'guild-1'))


if __name__ == '__main__':
    unittest.main()
