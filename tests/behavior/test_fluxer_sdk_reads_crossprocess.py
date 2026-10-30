"""Real private HTTP boundary from Python FluxerAdapter to SDK-model Node reads."""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from src.fluxer_adapter import FluxerAdapter
from src.fluxer_connector_config import FluxerConnectorConfig
from src.fluxer_rpc_client import FluxerRpcClient
from src.platform_types import ChannelRef, MessageRef, ThreadRef, PlatformOperationError


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
            rpc_url=f'http://127.0.0.1:{int(ready)}',
            bot_token='not-used', transport_secret='test-transport-secret',
        ))
        self.adapter = FluxerAdapter(connector_key='fluxer-contract',
                                     instance_origin='https://fluxer.example', rpc=self.rpc)
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
