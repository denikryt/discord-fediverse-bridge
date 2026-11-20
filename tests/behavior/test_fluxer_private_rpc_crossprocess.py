"""Real Python→Node authenticated RPC scenarios without a Fluxer SDK fake import."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

from src.fluxer_connector_config import FluxerConnectorConfig
from src.fluxer_rpc_client import FluxerRpcClient
from src.platform_types import PlatformOperationError


class FluxerRpcCrossProcessScenarios(unittest.IsolatedAsyncioTestCase):
    """Run a real loopback Node process with scoped HTTP rather than mock calls."""

    async def asyncSetUp(self) -> None:
        """Wait for the Node listener readiness line before touching its port."""
        root = Path(__file__).resolve().parents[2]
        self._node = await asyncio.create_subprocess_exec(
            'node', '--experimental-strip-types',
            str(root / 'fluxer-connector/tests/fixture_sidecar.mjs'),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._node.stdout is not None
        ready = await asyncio.wait_for(self._node.stdout.readline(), 8)
        if not ready.strip().isdigit():
            stderr = await self._node.stderr.read() if self._node.stderr else b''
            self.fail(f'Node private RPC listener failed: {stderr[:200]!r}')
        self.client = FluxerRpcClient(FluxerConnectorConfig(
            connector_key='fluxer-contract', origin='https://fluxer.example',
            rpc_url=f'http://127.0.0.1:{int(ready)}', transport_secret='test-transport-secret',
        ))

    async def asyncTearDown(self) -> None:
        """Stop RPC admission and release the child listener deterministically."""
        await self.client.close()
        if self._node.returncode is None:
            self._node.terminate()
        await asyncio.wait_for(self._node.wait(), 8)

    async def test_read_and_independent_starter_across_process(self) -> None:
        """One authenticated lookup and one write preserve physical thread/starter IDs."""
        channel = await self.client.call('fetch_channel', {'id': 'forum-1'})
        self.assertEqual(channel, {'id': 'forum-1', 'kind': 'forum', 'guild_id': 'guild-1'})
        created = await self.client.call('create_forum_thread', {'forum_id': 'forum-1'})
        self.assertEqual(created, {'thread_id': 'thread-1', 'starter_id': 'message-1'})
        self.assertNotEqual(created['thread_id'], created['starter_id'])

    async def test_invalid_read_and_unsupported_write_have_no_fallback(self) -> None:
        """A scoped error is preserved without changing modes or destinations."""
        with self.assertRaises(PlatformOperationError) as missing:
            await self.client.call('fetch_channel', {'id': 'other-forum'})
        self.assertEqual(missing.exception.reason, 'not_found')
        with self.assertRaises(PlatformOperationError) as unsupported:
            await self.client.call('send_message', {'destination': 'forum-1'})
        self.assertEqual(unsupported.exception.reason, 'unsupported')


if __name__ == '__main__':
    unittest.main()
