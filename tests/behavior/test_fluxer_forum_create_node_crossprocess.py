"""One bot forum post crosses Python adapter -> authenticated Node SDK REST RPC.

This test replaces only the external provider with a stateful SDK-edge fake;
production forum capability remains disabled until live server verification.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.fluxer_adapter import FluxerAdapter
from src.fluxer_connector_config import FluxerConnectorConfig
from src.fluxer_rpc_client import FluxerRpcClient
from src.platform_types import (
    ChannelRef, ContentDocument, CreateAttempt, DeliveryPresentation, MessageRef,
    PlatformOperationError, ThreadRef,
)


class FluxerForumCreateCrossProcess(unittest.IsolatedAsyncioTestCase):
    """Follow one complete physical Create and repeated attempt to the Node REST edge."""

    server_contract = 'explicit'
    starter_id = 'starter-91'

    async def asyncSetUp(self) -> None:
        """Boot a READY Node SDK session and a scoped authenticated Python client."""
        self.tmp = TemporaryDirectory()
        self.effects = Path(self.tmp.name) / 'effects.jsonl'
        self.acl_state = Path(self.tmp.name) / 'acl.json'
        self.acl_state.write_text(json.dumps({'forum_send': True, 'thread_send': True}))
        self.instance_origin = ('https://xn--fa-hia.test' if self._testMethodName ==
                                'test_ascii_alabel_matches_authenticated_node_origin'
                                else 'https://fx.test')
        root = Path(__file__).resolve().parents[2]
        self.node = await asyncio.create_subprocess_exec(
            'node', '--experimental-strip-types',
            str(root / 'fluxer-connector/tests/fixture_sdk_forum_create.mjs'),
            str(self.effects),
            ('lose-response' if self._testMethodName == 'test_lost_remote_response_is_unresolved_and_no_repost' else 'lose-message-response' if self._testMethodName == 'test_lost_reply_response_never_reposts' else 'normal'),
            self.server_contract, str(self.acl_state),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, 'FLUXER_TEST_ORIGIN': self.instance_origin},
        )
        assert self.node.stdout is not None
        line = await asyncio.wait_for(self.node.stdout.readline(), timeout=10)
        if not line.strip().isdigit():
            self.fail('Node SDK forum fixture did not start')
        self.rpc = FluxerRpcClient(FluxerConnectorConfig(
            'fx-forum', self.instance_origin, f'http://127.0.0.1:{int(line)}',
            'shared-ipc-secret',
        ))
        self.adapter = FluxerAdapter(
            connector_key='fx-forum', instance_origin=self.instance_origin,
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

    async def test_one_post_preserves_observed_ids_and_repeat_has_no_effect(self) -> None:
        """Observe one durable attempt's exact remote POST body and immutable receipt."""
        async def send():
            return await self.adapter.create_forum_thread(
                self.forum, ContentDocument('Hello from AP'), title='New topic',
                presentation=DeliveryPresentation('bot'), attempt=self.attempt,
            )

        receipt = await send()
        self.assertEqual(receipt.thread.external_thread_id, 'thread-9')
        self.assertEqual(receipt.starter, MessageRef('fx-forum', 'thread-9', self.starter_id))
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

    async def test_nested_reply_preserves_exact_thread_and_parent(self) -> None:
        """Observe one physical reply to the confirmed starter across processes."""
        root = await self.adapter.create_forum_thread(
            self.forum, ContentDocument('Hello from AP'), title='New topic',
            presentation=DeliveryPresentation('bot'), attempt=self.attempt,
        )
        attempt = CreateAttempt('child-reply-one', 'fx-forum', 'message', root.thread)
        async def send():
            return await self.adapter.send_message(
                root.thread, ContentDocument('@everyone replied safely'),
                reply_to=root.starter, presentation=DeliveryPresentation('bot'),
                attempt=attempt,
            )
        reply = await send()
        self.assertEqual(reply.message, MessageRef('fx-forum', 'thread-9', 'message-12'))
        self.assertEqual(reply, await send())
        effects = [json.loads(line) for line in self.effects.read_text().splitlines()]
        self.assertEqual(len(effects), 2)
        self.assertEqual(effects[1]['sdk_send']['container'], 'thread-9')
        self.assertEqual(effects[1]['sdk_send']['options']['replyTo'],
                         {'channelId': 'thread-9', 'messageId': self.starter_id,
                          'guildId': 'guild-1'})
        self.assertEqual(effects[1]['sdk_send']['options']['allowedMentions'],
                         {'parse': [], 'repliedUser': False})

    async def test_thread_acl_is_independent_of_parent_forum_send_permission(self) -> None:
        """Parent SendMessages revoke must not revoke bit-38 thread send at either boundary."""
        root = await self.adapter.create_forum_thread(
            self.forum, ContentDocument('root'), title='Forum root',
            presentation=DeliveryPresentation('bot'), attempt=self.attempt,
        )
        for index, (forum_send, thread_send, allowed) in enumerate((
            (True, True, True), (False, True, True), (True, False, False),
        )):
            with self.subTest(forum_send=forum_send, thread_send=thread_send):
                self.acl_state.write_text(json.dumps({
                    'forum_send': forum_send, 'thread_send': thread_send,
                }))
                # The second successful message replies to the immediately
                # preceding message, not to a synthetic top-level thread root.
                parent = (root.starter if index != 1 else
                          MessageRef('fx-forum', 'thread-9', 'message-12'))
                before = len([line for line in self.effects.read_text().splitlines()
                              if 'sdk_send' in json.loads(line)])
                attempt = CreateAttempt(f'acl-python-{index}', 'fx-forum',
                                        'message', root.thread)
                args = {
                    'container_id': 'thread-9', 'guild_id': 'guild-1',
                    'reply_to_message_id': parent.external_message_id,
                    'content': {'body': f'direct-{index}', 'format': 'plain'},
                    'allowed_mentions': {'parse': []},
                    'attempt_key': f'acl-node-{index}', 'delivery_mode': 'bot',
                }
                if allowed:
                    result = await self.adapter.send_message(
                        root.thread, ContentDocument(f'python-{index}'),
                        reply_to=parent, presentation=DeliveryPresentation('bot'),
                        attempt=attempt,
                    )
                    self.assertEqual(result.message.external_container_id, 'thread-9')
                    self.assertEqual((await self.rpc.call('send_message', args))['container_id'],
                                     'thread-9')
                    expected = before + 2
                else:
                    with self.assertRaises(PlatformOperationError) as python_rejected:
                        await self.adapter.send_message(
                            root.thread, ContentDocument(f'python-{index}'),
                            reply_to=parent, presentation=DeliveryPresentation('bot'),
                            attempt=attempt,
                        )
                    self.assertEqual(python_rejected.exception.reason, 'forbidden')
                    with self.assertRaises(PlatformOperationError) as node_rejected:
                        await self.rpc.call('send_message', args)
                    self.assertEqual(node_rejected.exception.reason, 'forbidden')
                    expected = before
                records = [json.loads(line)['sdk_send']
                           for line in self.effects.read_text().splitlines()
                           if 'sdk_send' in json.loads(line)]
                self.assertEqual(len(records), expected)
                if allowed:
                    self.assertEqual(records[-2]['options']['replyTo']['messageId'],
                                     parent.external_message_id)

    async def test_health_agrees_with_python_canonical_origin_only(self) -> None:
        """Canonical case/default-port spellings match; a different origin cannot attest."""
        from src.fluxer_connector_config import parse_fluxer_connectors
        base = {'connector_key': 'fx-forum', 'rpc_url': self.rpc._config.rpc_url,
                'transport_secret': 'shared-ipc-secret'}
        with self.assertRaises(ValueError):
            parse_fluxer_connectors(json.dumps([{**base, 'origin': 'https://fx.test.'}]))
        for raw, ready in (('https://FX.test:443', True),
                           ('https://other.test', False)):
            with self.subTest(origin=raw):
                cfg, = parse_fluxer_connectors(json.dumps([{**base, 'origin': raw}]))
                rpc = FluxerRpcClient(cfg)
                try:
                    adapter = FluxerAdapter(connector_key='fx-forum',
                                            instance_origin=cfg.origin, rpc=rpc)
                    self.assertEqual(await adapter.refresh_readiness(), ready)
                finally:
                    await rpc.close()

    async def test_ascii_alabel_matches_authenticated_node_origin(self) -> None:
        """An explicit IDN A-label must attest the same Python/Node identity."""
        from src.fluxer_connector_config import parse_fluxer_connectors

        base = {'connector_key': 'fx-forum', 'rpc_url': self.rpc._config.rpc_url,
                'transport_secret': 'shared-ipc-secret'}
        cfg, = parse_fluxer_connectors(json.dumps([
            {**base, 'origin': 'https://XN--FA-HIA.test:443'},
        ]))
        self.assertEqual(cfg.origin, 'https://xn--fa-hia.test')
        self.assertTrue(await self.adapter.refresh_readiness())
        # Unicode spelling must not get normalized to Python IDNA-2003's
        # different hostname, and that hostname must not attest Node READY.
        with self.assertRaises(ValueError):
            parse_fluxer_connectors(json.dumps([{**base, 'origin': 'https://faß.test'}]))
        wrong_cfg, = parse_fluxer_connectors(json.dumps([
            {**base, 'origin': 'https://fass.test'},
        ]))
        wrong_rpc = FluxerRpcClient(wrong_cfg)
        try:
            wrong_adapter = FluxerAdapter(connector_key='fx-forum',
                                          instance_origin=wrong_cfg.origin, rpc=wrong_rpc)
            self.assertFalse(await wrong_adapter.refresh_readiness())
        finally:
            await wrong_rpc.close()

    async def test_verified_thread_guild_fills_missing_hint_and_rejects_wrong_hint(self) -> None:
        """An omitted caller guild hint must not discard the live verified guild on write."""
        root = await self.adapter.create_forum_thread(
            self.forum, ContentDocument('root'), title='Topic',
            presentation=DeliveryPresentation('bot'), attempt=self.attempt,
        )
        no_hint = ThreadRef(ChannelRef('fx-forum', 'forum-1', 'forum'), 'thread-9')
        # Two nested sends use the same physically proven guild even though
        # neither caller-provided thread reference contains that hint.
        for index, parent in enumerate((root.starter,
                                        MessageRef('fx-forum', 'thread-9', 'message-12'))):
            receipt = await self.adapter.send_message(
                no_hint, ContentDocument(f'child {index}'), reply_to=parent,
                presentation=DeliveryPresentation('bot'),
                attempt=CreateAttempt(f'no-guild-{index}', 'fx-forum', 'message', no_hint),
            )
            self.assertEqual(receipt.message.external_container_id, 'thread-9')
        sends = [json.loads(line)['sdk_send'] for line in self.effects.read_text().splitlines()
                 if 'sdk_send' in json.loads(line)]
        self.assertEqual(len(sends), 2)
        self.assertEqual([send['options']['replyTo']['messageId'] for send in sends],
                         [self.starter_id, 'message-12'])
        self.assertEqual([send['options']['replyTo']['guildId'] for send in sends],
                         ['guild-1', 'guild-1'])
        # An explicit conflicting hint is not repaired silently by the server.
        foreign = ThreadRef(ChannelRef('fx-forum', 'forum-1', 'forum', 'foreign-guild'),
                            'thread-9')
        with self.assertRaises(PlatformOperationError) as caught:
            await self.adapter.send_message(
                foreign, ContentDocument('foreign'), reply_to=root.starter,
                presentation=DeliveryPresentation('bot'),
                attempt=CreateAttempt('foreign-hint', 'fx-forum', 'message', foreign),
            )
        self.assertEqual(caught.exception.reason, 'invalid_request')
        self.assertEqual(sum('sdk_send' in json.loads(line)
                             for line in self.effects.read_text().splitlines()), 2)

    async def test_lost_reply_response_never_reposts(self) -> None:
        """A lost reply ACK retains uncertain delivery without a second SDK send."""
        root = await self.adapter.create_forum_thread(
            self.forum, ContentDocument('Hello from AP'), title='New topic',
            presentation=DeliveryPresentation('bot'), attempt=self.attempt,
        )
        attempt = CreateAttempt('child-reply-lost', 'fx-forum', 'message', root.thread)
        for _ in range(2):
            with self.assertRaises(PlatformOperationError) as caught:
                await self.adapter.send_message(
                    root.thread, ContentDocument('unconfirmed reply'),
                    reply_to=root.starter, presentation=DeliveryPresentation('bot'),
                    attempt=attempt,
                )
            self.assertEqual(caught.exception.reason, 'unknown_delivery')
        effects = [json.loads(line) for line in self.effects.read_text().splitlines()]
        self.assertEqual(len(effects), 2)
        self.assertEqual(effects[1]['sdk_send']['options']['replyTo']['messageId'], self.starter_id)


class FluxerMeasuredRestCrossProcess(FluxerForumCreateCrossProcess):
    """Repeat all receipt/reply/no-repost scenarios with the measured REST shape.

    The server explicitly returns equal thread/starter IDs; messages omit guild
    on the wire (SDK null), and the raw starter omits webhook_id. Inherited tests
    keep the same observable assertions for both server response contracts.
    """

    server_contract = 'rest-omissions'
    starter_id = 'thread-9'
