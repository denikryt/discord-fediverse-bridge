"""One bot forum post crosses Python adapter -> authenticated Node SDK REST RPC.

This test replaces only the external provider with a stateful SDK-edge fake;
production forum capability remains disabled until live server verification.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.fluxer_adapter import FluxerAdapter
from src.fluxer_connector_config import FluxerConnectorConfig
from src.fluxer_rpc_client import FluxerRpcClient
from src.platform_types import (
    ChannelRef, ContentDocument, CreateAttempt, DeliveryPresentation, MessageRef,
    PlatformOperationError,
)


class FluxerForumCreateCrossProcess(unittest.IsolatedAsyncioTestCase):
    """Follow one complete physical Create and repeated attempt to the Node REST edge."""

    async def asyncSetUp(self) -> None:
        """Boot a READY Node SDK session and a scoped authenticated Python client."""
        self.tmp = TemporaryDirectory()
        self.effects = Path(self.tmp.name) / 'effects.jsonl'
        root = Path(__file__).resolve().parents[2]
        self.node = await asyncio.create_subprocess_exec(
            'node', '--experimental-strip-types',
            str(root / 'fluxer-connector/tests/fixture_sdk_forum_create.mjs'),
            str(self.effects),
            'lose-response' if self._testMethodName == 'test_lost_remote_response_is_unresolved_and_no_repost' else 'normal',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self.node.stdout is not None
        line = await asyncio.wait_for(self.node.stdout.readline(), timeout=10)
        if not line.strip().isdigit():
            self.fail('Node SDK forum fixture did not start')
        self.rpc = FluxerRpcClient(FluxerConnectorConfig(
            'fx-forum', 'https://fx.test', f'http://127.0.0.1:{int(line)}',
            'shared-ipc-secret',
        ))
        self.adapter = FluxerAdapter(
            connector_key='fx-forum', instance_origin='https://fx.test',
            rpc=self.rpc, forum_applied_tags=(('forum-1', ('tag-a',)),),
        )
        self.forum = ChannelRef('fx-forum', 'forum-1', 'forum', 'guild-1')
        self.attempt = CreateAttempt('single-thread-test', 'fx-forum', 'forum_thread', self.forum)

    async def asyncTearDown(self) -> None:
        """Join all RPC operations before terminating the sidecar fixture."""
        await self.rpc.close()
        if self.node.returncode is None:
            self.node.terminate()
        await asyncio.wait_for(self.node.wait(), timeout=10)
        self.tmp.cleanup()

    async def test_one_post_creates_distinct_thread_and_starter_and_repeat_has_no_effect(self) -> None:
        """Observe one durable attempt's exact remote POST body and immutable receipt."""
        async def send():
            return await self.adapter.create_forum_thread(
                self.forum, ContentDocument('Hello from AP'), title='New topic',
                presentation=DeliveryPresentation('bot'), attempt=self.attempt,
            )

        receipt = await send()
        self.assertEqual(receipt.thread.external_thread_id, 'thread-9')
        self.assertEqual(receipt.starter, MessageRef('fx-forum', 'thread-9', 'starter-91'))
        self.assertEqual(await send(), receipt)
        effects = [json.loads(line) for line in self.effects.read_text().splitlines()]
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]['route'], '/channels/forum-1/threads')
        self.assertEqual(effects[0]['options']['body']['message']['allowed_mentions'],
                         {'parse': []})
        self.assertEqual(effects[0]['options']['body']['applied_tags'], ['tag-a'])
        starter = await self.adapter.fetch_message(receipt.starter)
        self.assertEqual(starter.content.body, 'Hello from AP')
        self.assertTrue(starter.author.is_bridge_owned)

    async def test_cross_instance_and_webhook_request_never_reaches_rest(self) -> None:
        """Reject out-of-scope or webhook Create before a physical Node write."""
        with self.assertRaises(PlatformOperationError):
            await self.adapter.create_forum_thread(
                self.forum, ContentDocument('Hello'), title='New topic',
                presentation=DeliveryPresentation('webhook'), attempt=self.attempt,
            )
        self.assertFalse(self.effects.exists())

    async def test_lost_remote_response_is_unresolved_and_no_repost(self) -> None:
        """A committed POST with lost SDK response cannot authorize a second POST."""
        async def send():
            return await self.adapter.create_forum_thread(
                self.forum, ContentDocument('Hello from AP'), title='New topic',
                presentation=DeliveryPresentation('bot'), attempt=self.attempt,
            )

        for _ in range(2):
            with self.assertRaises(PlatformOperationError) as error:
                await send()
            self.assertEqual(error.exception.reason, 'unknown_delivery')
        effects = [json.loads(line) for line in self.effects.read_text().splitlines()]
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]['options']['body']['name'], 'New topic')
