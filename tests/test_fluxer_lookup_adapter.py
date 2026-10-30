"""Read-only Fluxer port tests with scoped provider replies and no guessed IDs."""

from __future__ import annotations

import unittest

import httpx

from src.fluxer_connector_config import FluxerConnectorConfig
from src.fluxer_adapter import FluxerAdapter
from src.fluxer_rpc_client import FluxerRpcClient
from src.platform_types import (
    AuthorRef, ChannelRef, MessageRef, PlatformOperationError, ThreadRef,
)


class FluxerLookupAdapterScenarios(unittest.IsolatedAsyncioTestCase):
    """Prove exact physical lookups before permitting canonical ownership."""

    def setUp(self) -> None:
        """Use one immutable connector and a stateful private RPC fake."""
        self.observed = []
        self.answers = {}

        def server(request):
            """Return explicit Node-normalized metadata only for known operations."""
            import json
            body = json.loads(request.content)
            self.observed.append(body)
            op = body['operation']
            result = self.answers.get(op)
            if result is None:
                return httpx.Response(403, json={'connector_key': 'fluxer-test', 'ok': False,
                                                 'error': {'reason': 'forbidden'}})
            return httpx.Response(200, json={'connector_key': 'fluxer-test',
                                             'ok': True, 'result': result})

        config = FluxerConnectorConfig('fluxer-test', 'https://fluxer.example',
                                       'http://127.0.0.1:8721', 'bot-token', 'transport-secret')
        self.rpc = FluxerRpcClient(config, transport=httpx.MockTransport(server))
        self.adapter = FluxerAdapter(connector_key='fluxer-test',
                                           instance_origin='https://fluxer.example', rpc=self.rpc)
        self.forum = ChannelRef('fluxer-test', 'forum-1', 'forum', 'guild-1')
        self.thread = ThreadRef(self.forum, 'thread-1')

    async def asyncTearDown(self) -> None:
        """Release private HTTP resources even after a failed read."""
        await self.rpc.close()

    async def test_exact_forum_thread_and_distinct_starter(self) -> None:
        """Independent starter identity and immediate forum parent survive reads."""
        self.answers['fetch_channel'] = {'id': 'forum-1', 'kind': 'forum',
                                         'guild_id': 'guild-1', 'name': 'Forum'}
        self.answers['fetch_thread'] = {'id': 'thread-1', 'parent_id': 'forum-1',
                                        'guild_id': 'guild-1', 'kind': 'forum',
                                        'starter_id': 'starter-9'}
        channel = await self.adapter.fetch_channel(self.forum)
        thread = await self.adapter.fetch_thread(self.thread)
        self.assertEqual(channel.channel, self.forum)
        self.assertEqual(thread.starter, MessageRef('fluxer-test', 'thread-1', 'starter-9'))
        self.assertNotEqual(thread.thread.external_thread_id, thread.starter.external_message_id)
        self.assertEqual([item['operation'] for item in self.observed],
                         ['fetch_channel', 'fetch_thread'])

    async def test_missing_starter_remains_unknown(self) -> None:
        """An absent provider starter ID is never invented from thread ID."""
        self.answers['fetch_thread'] = {'id': 'thread-1', 'parent_id': 'forum-1',
                                        'guild_id': 'guild-1', 'kind': 'forum'}
        thread = await self.adapter.fetch_thread(self.thread)
        self.assertIsNone(thread.starter)

    async def test_wrong_parent_guild_and_connector_fail_closed(self) -> None:
        """Provider mismatches cannot authorize a write to another forum."""
        self.answers['fetch_thread'] = {'id': 'thread-1', 'parent_id': 'other-forum',
                                        'guild_id': 'guild-1', 'kind': 'forum'}
        with self.assertRaises(PlatformOperationError):
            await self.adapter.fetch_thread(self.thread)
        self.answers['fetch_thread']['parent_id'] = 'forum-1'
        self.answers['fetch_thread']['guild_id'] = 'other-guild'
        with self.assertRaises(PlatformOperationError):
            await self.adapter.fetch_thread(self.thread)
        calls = len(self.observed)
        with self.assertRaises(PlatformOperationError):
            await self.adapter.fetch_channel(ChannelRef('another', 'forum-1'))
        self.assertEqual(len(self.observed), calls)

    async def test_message_container_author_and_timestamp_are_scoped(self) -> None:
        """A correct message read stays in its immediate thread container."""
        self.answers['fetch_message'] = {
            'id': 'message-1', 'container_id': 'thread-1', 'content': 'Hello',
            'author': {'id': 'author-7', 'kind': 'human', 'display_name': 'Author'},
            'created_at': '2026-10-26T11:00:00Z',
        }
        message = MessageRef('fluxer-test', 'thread-1', 'message-1')
        result = await self.adapter.fetch_message(message)
        self.assertEqual(result.message, message)
        self.assertEqual(result.content.body, 'Hello')
        self.assertEqual(result.author.external_actor_id, 'author-7')
        self.answers['fetch_message']['container_id'] = 'other-thread'
        with self.assertRaises(PlatformOperationError):
            await self.adapter.fetch_message(message)
        self.assertEqual(len(self.observed), 2)

    async def test_bot_capabilities_webhook_false_and_unknown_acl(self) -> None:
        """Unavailable bot membership and webhook options never become grants."""
        self.answers['capabilities'] = {
            'channel_id': 'forum-1', 'guild_id': 'guild-1',
            'forum_posts': True, 'message_threads': False, 'thread_replies': True,
            'attachments': False, 'message_edit_delete': True,
            'webhook_send': True, 'webhook_edit_delete': True,
        }
        caps = await self.adapter.capabilities(self.forum)
        self.assertTrue(caps.forum_posts)
        self.assertFalse(caps.webhook_send)
        self.assertFalse(caps.webhook_edit_delete)
        principal = AuthorRef('fluxer-test', 'bot-1', 'bot', 'Bridge')
        grants = await self.adapter.check_permissions(
            principal, self.forum, frozenset({'view_channel', 'send_message'}))
        self.assertEqual(grants, {'view_channel': 'unknown', 'send_message': 'unknown'})

    async def test_bot_acl_requires_exact_attested_identity_and_channel(self) -> None:
        """Only a response for the connected bot and queried forum may authorize."""
        self.adapter._bot_id = 'bot-1'
        self.answers['health'] = {'connector_key':'fluxer-test', 'state':'ready', 'bot_id':'bot-1'}
        self.answers['check_permissions'] = {
            'bot_id': 'bot-1', 'channel_id': 'forum-1', 'guild_id': 'guild-1',
            'view_channel': 'allowed', 'send_message': 'denied',
        }
        bot = AuthorRef('fluxer-test', 'bot-1', 'bot', 'Bridge')
        actions = frozenset({'view_channel', 'send_message'})
        result = await self.adapter.check_permissions(bot, self.forum, actions)
        self.assertEqual(result, {'view_channel': 'allowed', 'send_message': 'denied'})
        self.answers['check_permissions']['channel_id'] = 'different-forum'
        result = await self.adapter.check_permissions(bot, self.forum, actions)
        self.assertEqual(result, {'view_channel': 'unknown', 'send_message': 'unknown'})

    async def test_capability_scope_mismatch_cannot_advertise_forum_write(self) -> None:
        """A valid-looking capability flag from another forum is not authority."""
        self.answers['capabilities'] = {
            'channel_id': 'other-forum', 'guild_id': 'guild-1',
            'forum_posts': True,
        }
        caps = await self.adapter.capabilities(self.forum)
        self.assertFalse(caps.forum_posts)

    async def test_unsupported_kind_and_invalid_acl_never_grant(self) -> None:
        """An unrecognized native type/permission cannot be promoted to forum/allowed."""
        self.answers['fetch_channel'] = {'id': 'forum-1', 'kind': 'mystery',
                                         'guild_id': 'guild-1'}
        with self.assertRaises(PlatformOperationError):
            await self.adapter.fetch_channel(self.forum)
        self.answers['check_permissions'] = {'view_channel': 'yes'}
        principal = AuthorRef('fluxer-test', 'bot-1', 'bot', 'Bridge')
        acl = await self.adapter.check_permissions(
            principal, self.forum, frozenset({'view_channel'}))
        self.assertEqual(acl, {'view_channel': 'unknown'})


if __name__ == '__main__':
    unittest.main()
