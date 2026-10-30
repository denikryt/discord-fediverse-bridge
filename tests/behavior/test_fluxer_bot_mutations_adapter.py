"""Bot-owned Fluxer edits/deletes preserve exact message scope and ACL admission."""

from __future__ import annotations

import asyncio
import json
import unittest
import httpx

from src.fluxer_adapter import FluxerAdapter
from src.fluxer_connector_config import FluxerConnectorConfig
from src.fluxer_rpc_client import FluxerRpcClient
from src.platform_types import ContentDocument, MessageRef, PlatformOperationError


class StatefulMutationProvider:
    """Persist physical remote state; only the outer Node SDK remains fake."""

    def __init__(self):
        """Provide two messages with distinct verified ownership."""
        self.messages = {
            ('thread-1', 'bot-message'): {'content': 'before', 'author_id': 'bot-1', 'kind': 'bot'},
            ('thread-1', 'foreign-message'): {'content': 'foreign', 'author_id': 'human-7', 'kind': 'human'},
            ('thread-2', 'bot-message'): {'content': 'unrelated', 'author_id': 'bot-1', 'kind': 'bot'},
        }
        self.requests = []
        self.writes = []
        self.permission = 'allowed'
        self.fail_write = False

    def serve(self, request):
        """Read current remote state or mutate only a concrete physical key."""
        body = json.loads(request.content)
        self.requests.append(body)
        op, args = body['operation'], body['args']
        if op == 'fetch_message':
            key = args['container_id'], args['id']
            state = self.messages.get(key)
            if state is None:
                return httpx.Response(404, json={'connector_key': 'fluxer-test',
                                                 'ok': False, 'error': {'reason': 'not_found'}})
            result = {'container_id': key[0], 'id': key[1], 'content': state['content'],
                      'author': {'id': state['author_id'], 'kind': state['kind']}}
        elif op == 'fetch_channel':
            result = {'id': args['id'], 'kind': 'text', 'guild_id': 'guild-1'}
        elif op == 'capabilities':
            result = {'channel_id': args['channel_id'], 'guild_id': 'guild-1',
                      'message_edit_delete': True}
        elif op == 'check_permissions':
            result = {'bot_id': 'bot-1', 'channel_id': args['channel_id'],
                      'guild_id': 'guild-1', **{action: self.permission for action in args['actions']}}
        elif op in ('edit_message', 'delete_message'):
            key = args['container_id'], args['message_id']
            self.writes.append(body)
            state = self.messages.get(key)
            if self.fail_write:
                raise httpx.ReadError('response lost after provider write')
            if state is None:
                return httpx.Response(404, json={'connector_key': 'fluxer-test',
                                                 'ok': False, 'error': {'reason': 'not_found'}})
            if op == 'edit_message':
                state['content'] = args['content']['body']
            else:
                del self.messages[key]
            result = {'container_id': key[0], 'message_id': key[1]}
        else:
            raise AssertionError('unexpected RPC method')
        return httpx.Response(200, json={'connector_key': 'fluxer-test',
                                         'ok': True, 'result': result})


class FluxerBotMutationScenarios(unittest.IsolatedAsyncioTestCase):
    """Drive physical edits/deletes through scoped RPC and a stateful provider."""

    async def asyncSetUp(self):
        """Initialize a non-Discord adapter and concrete mutable remote state."""
        self.provider = StatefulMutationProvider()
        config = FluxerConnectorConfig('fluxer-test', 'https://fluxer.example',
                                       'http://127.0.0.1:8721', 'bot-secret', 'transport-secret')
        self.rpc = FluxerRpcClient(config, transport=httpx.MockTransport(self.provider.serve))
        self.adapter = FluxerAdapter(connector_key='fluxer-test',
                                     instance_origin='https://fluxer.example',
                                     rpc=self.rpc, bot_actor_id='bot-1')
        self.owned = MessageRef('fluxer-test', 'thread-1', 'bot-message')
        self.foreign = MessageRef('fluxer-test', 'thread-1', 'foreign-message')

    async def asyncTearDown(self):
        """Release the private RPC client and its accepted work."""
        await self.rpc.close()

    async def test_bot_owned_edit_only_changes_target_and_preserves_other_copy(self):
        """One verified edit has exactly one remote effect in the target container."""
        await self.adapter.edit_message(self.owned, ContentDocument('after'))
        self.assertEqual(self.provider.messages[('thread-1', 'bot-message')]['content'], 'after')
        self.assertEqual(self.provider.messages[('thread-2', 'bot-message')]['content'], 'unrelated')
        self.assertEqual(len(self.provider.writes), 1)
        self.assertEqual(self.provider.writes[0]['args']['message_id'], 'bot-message')
        self.assertEqual(self.provider.writes[0]['args']['container_id'], 'thread-1')
        self.assertEqual(self.provider.writes[0]['args']['allowed_mentions'], {'parse': []})

    async def test_delete_never_removes_container_or_foreign_message(self):
        """Delete targets only exact bot-owned message rather than its thread."""
        await self.adapter.delete_message(self.owned)
        self.assertNotIn(('thread-1', 'bot-message'), self.provider.messages)
        self.assertIn(('thread-1', 'foreign-message'), self.provider.messages)
        self.assertIn(('thread-2', 'bot-message'), self.provider.messages)
        self.assertEqual(len(self.provider.writes), 1)
        self.assertEqual(self.provider.writes[0]['operation'], 'delete_message')

    async def test_foreign_author_denied_before_write(self):
        """A valid physical ID is not authority to overwrite a human message."""
        for operation in ('edit', 'delete'):
            with self.subTest(operation=operation), self.assertRaises(PlatformOperationError) as caught:
                if operation == 'edit':
                    await self.adapter.edit_message(self.foreign, ContentDocument('changed'))
                else:
                    await self.adapter.delete_message(self.foreign)
            self.assertEqual(caught.exception.reason, 'forbidden')
        self.assertEqual(self.provider.writes, [])

    async def test_wrong_connector_missing_message_and_revoked_acl_fail_closed(self):
        """Unverified identity/permissions cannot cause a write to another target."""
        with self.assertRaises(PlatformOperationError):
            await self.adapter.delete_message(MessageRef('discord-primary', 'thread-1', 'bot-message'))
        with self.assertRaises(PlatformOperationError) as missing:
            await self.adapter.delete_message(MessageRef('fluxer-test', 'thread-1', 'missing'))
        self.assertEqual(missing.exception.reason, 'not_found')
        self.provider.permission = 'denied'
        with self.assertRaises(PlatformOperationError) as denied:
            await self.adapter.edit_message(self.owned, ContentDocument('changed'))
        self.assertEqual(denied.exception.reason, 'forbidden')
        self.assertEqual(self.provider.writes, [])

    async def test_invalid_format_and_missing_attested_bot_refuse(self):
        """Unsupported content and unknown principal never produce remote effects."""
        with self.assertRaises(PlatformOperationError) as unsupported:
            await self.adapter.edit_message(self.owned, ContentDocument('**rich**', 'portable_markdown'))
        self.assertEqual(unsupported.exception.reason, 'unsupported')
        self.adapter._bot_id = None
        with self.assertRaises(PlatformOperationError):
            await self.adapter.delete_message(self.owned)
        self.assertEqual(self.provider.writes, [])

    async def test_shutdown_joins_inflight_mutation_before_refusing_new(self):
        """Quiesce cannot close SQLite while an admitted edit remains in flight."""
        original = self.provider.serve
        entered = asyncio.Event()
        release = asyncio.Event()

        async def held_write(request):
            """Hold the write after admission without faking an SDK reply."""
            if json.loads(request.content)['operation'] == 'edit_message':
                entered.set()
                await release.wait()
            return original(request)

        self.rpc._http._transport = httpx.MockTransport(held_write)
        editing = asyncio.create_task(self.adapter.edit_message(
            self.owned, ContentDocument('after')))
        await entered.wait()
        closing = asyncio.create_task(self.adapter.quiesce())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        with self.assertRaises(PlatformOperationError) as stopped:
            await self.adapter.delete_message(self.owned)
        self.assertEqual(stopped.exception.reason, 'unavailable')
        release.set()
        await asyncio.gather(editing, closing)
        self.assertEqual(self.provider.messages[('thread-1', 'bot-message')]['content'], 'after')
        self.assertEqual(len(self.provider.writes), 1)

    async def test_malformed_success_after_write_is_unknown(self):
        """A 200 response for the wrong physical copy is not an ACK for our target."""
        original = self.provider.serve

        def wrong_ack(request):
            """Simulate a provider writing locally but acknowledging another message."""
            response = original(request)
            if json.loads(request.content)['operation'] == 'edit_message':
                payload = json.loads(response.content)
                payload['result']['message_id'] = 'unrelated-message'
                return httpx.Response(200, json=payload)
            return response

        self.rpc._http._transport = httpx.MockTransport(wrong_ack)
        with self.assertRaises(PlatformOperationError) as uncertain:
            await self.adapter.edit_message(self.owned, ContentDocument('after'))
        self.assertEqual(uncertain.exception.reason, 'unknown_delivery')
        self.assertEqual(len(self.provider.writes), 1)

    async def test_lost_write_result_never_triggers_adapter_retry(self):
        """Unknown update/delete outcome bubbles to durable mutation coordinator."""
        self.provider.fail_write = True
        with self.assertRaises(PlatformOperationError) as caught:
            await self.adapter.edit_message(self.owned, ContentDocument('changed'))
        self.assertEqual(caught.exception.reason, 'unknown_delivery')
        self.assertEqual(len(self.provider.writes), 1)


if __name__ == '__main__':
    unittest.main()
