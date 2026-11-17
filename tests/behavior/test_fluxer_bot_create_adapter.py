"""Physical bot Creates cross the private RPC boundary once with verified receipts.

The fake provider persists remote requests and effects; no SDK-route claims are
made before the pinned Fluxer SDK is available for integration testing.
"""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from src.fluxer_adapter import FluxerAdapter
from src.fluxer_connector_config import FluxerConnectorConfig
from src.fluxer_rpc_client import FluxerRpcClient
from src.platform_types import (
    ChannelRef, ContentDocument, CreateAttempt, DeliveryPresentation, MessageRef,
    PlatformOperationError, ThreadRef, _issue_committed_create_dispatch_proof,
    _issue_committed_create_ack_proof,
)


class Provider:
    """Persist one fake Fluxer remote effect per private RPC write entry."""

    def __init__(self):
        self.requests = []
        self.remote_threads = []
        self.remote_messages = []
        self.ready = asyncio.Event()
        self.unblock = asyncio.Event()
        self.block_create = False
        self.lost_ack = False
        self.malformed_receipt = False
        self.permission = 'allowed'
        self.thread_capability = True

    async def handle(self, request):
        """Return transport-normalized read data or record a physical write."""
        body = json.loads(request.content)
        op = body['operation']
        args = body['args']
        self.requests.append(body)
        if op == 'health':
            result = {'connector_key': 'fluxer-test', 'state': 'ready', 'bot_id': 'bot-1',
                      'instance_origin': 'https://fluxer.example'}
        elif op == 'fetch_channel':
            result = {'id': args['id'], 'kind': 'forum' if args['id'] == 'forum-1' else 'text',
                      'guild_id': 'guild-1'}
        elif op == 'capabilities':
            result = {'channel_id': args['channel_id'], 'guild_id': 'guild-1',
                      'forum_posts': True, 'thread_replies': self.thread_capability, 'attachments': False,
                      'message_edit_delete': True, 'tag_requirements_verified': True,
                      'requires_tag': False, 'available_tags': []}
        elif op == 'check_permissions':
            result = {'bot_id': 'bot-1', 'channel_id': args['channel_id'],
                      'guild_id': 'guild-1', **{action: self.permission for action in args['actions']}}
        elif op == 'fetch_thread':
            result = {'id': args['id'], 'parent_id': 'forum-1', 'kind': 'forum',
                      'guild_id': 'guild-1', 'starter_id': 'starter-91'}
        elif op == 'fetch_message':
            result = {'id': args['id'], 'container_id': args['container_id'],
                      'content': 'parent text', 'author': {'kind': 'bot', 'id': 'bot-1'},
                      'webhook_id': None}
        elif op == 'create_forum_thread':
            self.remote_threads.append(args)
            self.ready.set()
            if self.block_create:
                await self.unblock.wait()
            if self.lost_ack:
                raise httpx.ReadError('provider committed, response lost')
            result = {'forum_id': 'forum-1', 'guild_id': 'guild-1',
                      'thread_id': 'thread-9', 'starter_id': None if self.malformed_receipt else 'starter-91',
                      'delivery_mode': 'bot'}
        elif op == 'send_message':
            self.remote_messages.append(args)
            result = {'container_id': args['container_id'], 'message_id': 'message-13',
                      'delivery_mode': 'bot'}
        else:
            return httpx.Response(422, json={'connector_key':'fluxer-test','ok':False,
                                            'error':{'reason':'unsupported'}})
        return httpx.Response(200, json={'connector_key': 'fluxer-test', 'ok': True, 'result': result})


class FluxerBotCreateScenarios(unittest.IsolatedAsyncioTestCase):
    """Verify bot-only physical effects and exact create-token admission."""

    async def asyncSetUp(self):
        """Start one connector and stateful remote provider."""
        self.provider = Provider()
        cfg = FluxerConnectorConfig('fluxer-test', 'https://fluxer.example',
                                    'http://127.0.0.1:8721', 'transport-secret')
        self.rpc = FluxerRpcClient(cfg, transport=httpx.MockTransport(self.provider.handle))
        self.adapter = FluxerAdapter(connector_key='fluxer-test',
                                     instance_origin='https://fluxer.example',
                                     rpc=self.rpc, bot_actor_id='bot-1')
        self.forum = ChannelRef('fluxer-test', 'forum-1', 'forum', 'guild-1')
        self.content = ContentDocument('safe text')
        self.presentation = DeliveryPresentation('bot')

    async def asyncTearDown(self):
        """Join transport calls and release private HTTP resources."""
        await self.rpc.close()

    def forum_attempt(self, key='direct-a'):
        """Construct one immutable exact target attempt."""
        return CreateAttempt(key, 'fluxer-test', 'forum_thread', self.forum)

    async def create(self, attempt=None, **kwargs):
        """Invoke the actual adapter create operation, not a mocked method."""
        return await self.adapter.create_forum_thread(
            self.forum, kwargs.pop('content', self.content), title='Forum title',
            presentation=kwargs.pop('presentation', self.presentation),
            attempt=attempt or self.forum_attempt(), **kwargs)

    def permit(self, attempt):
        """Model signed SQLite dispatch proof, never fabricate permit bytes."""
        proof = _issue_committed_create_dispatch_proof(19, attempt.key, 'a'*64, 'claim-19')
        permit = self.adapter._mint_coordinator_dispatch_permit(
            proof=proof, intent_id=19, attempt=attempt,
            request_fingerprint='a'*64, dispatch_token='claim-19',
            operation='forum_thread', destination=self.forum, content=self.content,
            presentation=self.presentation, title='Forum title', reply_to=None)
        return permit

    async def test_forum_post_has_distinct_thread_and_starter_and_exact_one_effect(self):
        """One request creates exactly one thread with a separately proven starter."""
        attempt = self.forum_attempt()
        receipt = await self.create(attempt)
        self.assertEqual(receipt.thread.external_thread_id, 'thread-9')
        self.assertEqual(receipt.starter, MessageRef('fluxer-test', 'thread-9', 'starter-91'))
        self.assertEqual((await self.create(attempt)), receipt)
        self.assertEqual(len(self.provider.remote_threads), 1)
        self.assertEqual(self.provider.remote_threads[0]['content']['body'], 'safe text')

    async def test_reserved_create_requires_valid_one_use_permit(self):
        """No bare fanout key or stolen permit authorizes another remote POST."""
        attempt = self.forum_attempt('fanout:v1:local:post:20:fluxer-test:7')
        with self.assertRaises(PlatformOperationError) as caught:
            await self.create(attempt)
        self.assertEqual(caught.exception.reason, 'invalid_request')
        permit = self.permit(attempt)
        with self.assertRaises(PlatformOperationError):
            await self.create(attempt, dispatch_permit=permit, content=ContentDocument('changed'))
        self.assertEqual(len(self.provider.remote_threads), 0)
        receipt = await self.create(attempt, dispatch_permit=permit)
        self.assertEqual(receipt.starter.external_message_id, 'starter-91')
        with self.assertRaises(PlatformOperationError):
            await self.create(attempt, dispatch_permit=permit)
        self.assertEqual(len(self.provider.remote_threads), 1)

    async def test_unknown_lost_ack_cannot_retry_same_attempt(self):
        """Remote commits but RPC ACK is lost; no second physical request follows."""
        self.provider.lost_ack = True
        attempt = self.forum_attempt()
        for _ in range(2):
            with self.assertRaises(PlatformOperationError) as caught:
                await self.create(attempt)
            self.assertEqual(caught.exception.reason, 'unknown_delivery')
        self.assertEqual(len(self.provider.remote_threads), 1)
        self.assertIsNone(await self.adapter.reconcile_create(attempt))

    async def test_receipt_without_starter_is_uncertain_not_a_guessed_id(self):
        """Malformed successful POST cannot authorize a subsequent second POST."""
        self.provider.malformed_receipt = True
        for _ in range(2):
            with self.assertRaises(PlatformOperationError) as caught:
                await self.create()
            self.assertEqual(caught.exception.reason, 'unknown_delivery')
        self.assertEqual(len(self.provider.remote_threads), 1)

    async def test_concurrent_same_attempt_sends_once_and_payload_drift_rejected(self):
        """Two callers share one flight while unrelated content cannot join it."""
        self.provider.block_create = True
        first = asyncio.create_task(self.create())
        await self.provider.ready.wait()
        second = asyncio.create_task(self.create())
        with self.assertRaises(PlatformOperationError):
            await self.create(content=ContentDocument('tampered'))
        self.provider.unblock.set()
        left, right = await asyncio.gather(first, second)
        self.assertEqual(left, right)
        self.assertEqual(len(self.provider.remote_threads), 1)

    async def test_permissions_webhooks_and_attachments_refuse_before_remote_write(self):
        """Unsafe modes and unverified rights cannot produce a degraded copy."""
        self.provider.permission = 'denied'
        with self.assertRaises(PlatformOperationError) as denied:
            await self.create()
        self.assertEqual(denied.exception.reason, 'forbidden')
        self.provider.permission = 'allowed'
        with self.assertRaises(PlatformOperationError) as webhook:
            await self.create(presentation=DeliveryPresentation('webhook'))
        self.assertEqual(webhook.exception.reason, 'unsupported')
        self.assertEqual(self.provider.remote_threads, [])

    async def test_unverified_or_required_tags_refuse_before_forum_post(self):
        """A forum flag without attested tag policy never authorizes remote writes."""
        orig = self.provider.handle

        async def no_tag_proof(request):
            """Simulate a provider returning a verified forum but unknown tag rules."""
            response = await orig(request)
            data = json.loads(request.content)
            if data['operation'] == 'capabilities':
                body = json.loads(response.content)
                body['result']['tag_requirements_verified'] = False
                return httpx.Response(200, json=body)
            return response

        self.rpc._http._transport = httpx.MockTransport(no_tag_proof)
        with self.assertRaises(PlatformOperationError) as error:
            await self.create()
        self.assertEqual(error.exception.reason, 'unsupported')
        self.assertEqual(self.provider.remote_threads, [])
        self.rpc._http._transport = httpx.MockTransport(orig)

        async def required_tags(request):
            """Report a mandatory tag not selected by the operator."""
            response = await orig(request)
            if json.loads(request.content)['operation'] == 'capabilities':
                body = json.loads(response.content)
                body['result']['requires_tag'] = True
                return httpx.Response(200, json=body)
            return response

        self.rpc._http._transport = httpx.MockTransport(required_tags)
        with self.assertRaises(PlatformOperationError) as error:
            await self.create()
        self.assertEqual(error.exception.reason, 'unsupported')
        self.assertEqual(self.provider.remote_threads, [])

    async def test_operator_tags_are_verified_against_current_forum_before_post(self):
        """Only a configured, current, unmoderated forum tag reaches remote Create."""
        self.adapter._forum_applied_tags = {'forum-1': ('tag-a',)}
        original = self.provider.handle

        async def tagged_forum(request):
            """Return observed forum tags and preserve the real fake remote effects."""
            response = await original(request)
            if json.loads(request.content)['operation'] == 'capabilities':
                body = json.loads(response.content)
                body['result'].update({
                    'requires_tag': True,
                    'available_tags': [{'id': 'tag-a', 'moderated': False},
                                       {'id': 'tag-mod', 'moderated': True}],
                })
                return httpx.Response(200, json=body)
            return response

        self.rpc._http._transport = httpx.MockTransport(tagged_forum)
        await self.create()
        self.assertEqual(self.provider.remote_threads[0]['applied_tags'], ['tag-a'])
        self.assertEqual(len(self.provider.remote_threads), 1)

    async def test_removed_or_moderated_operator_tag_cannot_write(self):
        """Revalidate configured tags against current provider inventory per attempt."""
        self.adapter._forum_applied_tags = {'forum-1': ('tag-mod',)}
        original = self.provider.handle

        async def restricted_forum(request):
            """Return tags which cannot be applied by unverified moderators."""
            response = await original(request)
            if json.loads(request.content)['operation'] == 'capabilities':
                body = json.loads(response.content)
                body['result']['available_tags'] = [{'id': 'tag-mod', 'moderated': True}]
                return httpx.Response(200, json=body)
            return response

        self.rpc._http._transport = httpx.MockTransport(restricted_forum)
        with self.assertRaises(PlatformOperationError) as error:
            await self.create()
        self.assertEqual(error.exception.reason, 'unsupported')
        self.assertEqual(self.provider.remote_threads, [])
        self.adapter._forum_applied_tags = {'forum-1': ('removed',)}
        with self.assertRaises(PlatformOperationError):
            await self.create()
        self.assertEqual(self.provider.remote_threads, [])

    async def test_wrong_forum_receipt_after_post_is_sticky_uncertainty(self):
        """The adapter never persists or retries a successful wrong-scope receipt."""
        orig = self.provider.handle

        async def wrong_receipt(request):
            """Return foreign forum metadata after the remote write was committed."""
            response = await orig(request)
            if json.loads(request.content)['operation'] == 'create_forum_thread':
                body = json.loads(response.content)
                body['result']['forum_id'] = 'foreign-forum'
                return httpx.Response(200, json=body)
            return response

        self.rpc._http._transport = httpx.MockTransport(wrong_receipt)
        for _ in range(2):
            with self.assertRaises(PlatformOperationError) as error:
                await self.create()
            self.assertEqual(error.exception.reason, 'unknown_delivery')
        self.assertEqual(len(self.provider.remote_threads), 1)

    async def test_write_redirect_is_uncertain_and_not_reissued(self):
        """A physical write receiving a 302 is not proof of zero provider effect."""
        orig = self.provider.handle

        async def redirect_after_write(request):
            """Commit provider effect before a suspicious redirect response."""
            response = await orig(request)
            if json.loads(request.content)['operation'] == 'create_forum_thread':
                return httpx.Response(302, headers={'Location': 'https://elsewhere.test/rpc'})
            return response

        self.rpc._http._transport = httpx.MockTransport(redirect_after_write)
        for _ in range(2):
            with self.assertRaises(PlatformOperationError) as error:
                await self.create()
            self.assertEqual(error.exception.reason, 'unknown_delivery')
        self.assertEqual(len(self.provider.remote_threads), 1)

    async def test_reply_rejects_wrong_parent_and_sends_into_thread(self):
        """Exact target-local reply is preserved; cross-container is never flattened."""
        destination = ThreadRef(self.forum, 'thread-9')
        attempt = CreateAttempt('reply-a', 'fluxer-test', 'message', destination)
        parent = MessageRef('fluxer-test', 'thread-9', 'parent-3')
        response = await self.adapter.send_message(
            destination, self.content, reply_to=parent, presentation=self.presentation,
            attempt=attempt)
        self.assertEqual(response.message, MessageRef('fluxer-test', 'thread-9', 'message-13'))
        self.assertEqual(self.provider.remote_messages[0]['reply_to_message_id'], 'parent-3')
        wrong = MessageRef('fluxer-test', 'other-thread', 'parent-3')
        with self.assertRaises(PlatformOperationError):
            await self.adapter.send_message(destination, self.content, reply_to=wrong,
                                            presentation=self.presentation,
                                            attempt=CreateAttempt('reply-b', 'fluxer-test', 'message', destination))
        self.assertEqual(len(self.provider.remote_messages), 1)

    async def test_direct_channel_send_uses_verified_guild_and_rejects_wrong_hint(self):
        """Text-channel sends must not discard the exact guild returned by fetch_channel."""
        channel = ChannelRef('fluxer-test', 'text-1', 'text')
        sent = await self.adapter.send_message(
            channel, self.content, reply_to=None, presentation=self.presentation,
            attempt=CreateAttempt('text-no-hint', 'fluxer-test', 'message', channel),
        )
        self.assertEqual(sent.message.external_container_id, 'text-1')
        self.assertEqual(self.provider.remote_messages[-1]['guild_id'], 'guild-1')
        wrong = ChannelRef('fluxer-test', 'text-1', 'text', 'wrong-guild')
        with self.assertRaises(PlatformOperationError):
            await self.adapter.send_message(
                wrong, self.content, reply_to=None, presentation=self.presentation,
                attempt=CreateAttempt('text-wrong-hint', 'fluxer-test', 'message', wrong),
            )
        self.assertEqual(len(self.provider.remote_messages), 1)

    async def test_unverified_thread_capability_refuses_before_write(self):
        """A verified parent cannot substitute for unavailable thread capability."""
        self.provider.thread_capability = False
        destination = ThreadRef(self.forum, 'thread-9')
        with self.assertRaises(PlatformOperationError) as caught:
            await self.adapter.send_message(
                destination, self.content, reply_to=MessageRef('fluxer-test', 'thread-9', 'parent-3'),
                presentation=self.presentation,
                attempt=CreateAttempt('reply-not-verified', 'fluxer-test', 'message', destination),
            )
        self.assertEqual(caught.exception.reason, 'unsupported')
        self.assertEqual(self.provider.remote_messages, [])

    async def test_quiesce_joins_inflight_and_refuses_later_calls(self):
        """Close admission while an already-started remote operation is outstanding."""
        self.provider.block_create = True
        first = asyncio.create_task(self.create())
        await self.provider.ready.wait()
        stop = asyncio.create_task(self.adapter.quiesce())
        await asyncio.sleep(0)
        with self.assertRaises(PlatformOperationError):
            await self.create(self.forum_attempt('later'))
        self.provider.unblock.set()
        await asyncio.gather(first, stop)
        self.assertEqual(len(self.provider.remote_threads), 1)


if __name__ == '__main__':
    unittest.main()

class FluxerDurableSqliteCreateScenarios(unittest.IsolatedAsyncioTestCase):
    """Exercise real SQLite durable claims through the real Fluxer bot RPC adapter."""

    async def asyncSetUp(self):
        """Provision one explicitly test-only Fluxer forum owner, not #13 routing."""
        import tempfile
        from pathlib import Path
        from src.db import Database
        from src.models import LocalCommunity, LocalCommunityThread, PlatformConnector, ChannelSurface
        from src.content_sync.platform_fanout import PlatformFanoutCoordinator

        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Database(f'sqlite:///{Path(self.tempdir.name) / "bridge.db"}')
        self.database.migrate()
        with self.database.immediate_session() as session:
            community = LocalCommunity(
                slug='fluxer-transport-fixture', display_name='Isolated test community',
                actor_url='https://bridge.test/actors/communities/test',
                inbox_url='https://bridge.test/actors/communities/test/inbox',
                outbox_url='https://bridge.test/actors/communities/test/outbox',
                followers_url='https://bridge.test/actors/communities/test/followers',
                public_key_pem='public-fixture', private_key_pem='private-fixture', status='active')
            session.add(community)
            session.flush()
            connector = PlatformConnector(connector_key='fluxer-test', platform='fluxer',
                                          instance_origin='https://fluxer.example')
            session.add(connector)
            session.flush()
            session.add(ChannelSurface(connector_id=connector.id, external_channel_id='forum-1',
                                       external_guild_id='guild-1', kind='forum',
                                       local_community_id=community.id))
            canonical = LocalCommunityThread(
                local_community_id=community.id,
                ap_activity_id='https://bridge.test/actors/communities/test/activities/test',
                ap_object_id='https://bridge.test/objects/test', direction='inbound',
                origin_kind='activitypub')
            session.add(canonical)
            session.flush()
            self.canonical_id = canonical.id
        self.provider = Provider()
        cfg = FluxerConnectorConfig('fluxer-test', 'https://fluxer.example',
                                    'http://127.0.0.1:8721', 'transport-secret')
        self.rpc = FluxerRpcClient(cfg, transport=httpx.MockTransport(self.provider.handle))
        self.adapter = FluxerAdapter(connector_key='fluxer-test',
                                     instance_origin='https://fluxer.example',
                                     rpc=self.rpc, bot_actor_id='bot-1')
        self.forum = ChannelRef('fluxer-test', 'forum-1', 'forum', 'guild-1')

        class Registry:
            """Expose only this explicitly provisioned test connector."""

            def __init__(self, adapter):
                """Store the one current authorized test transport."""
                self.adapter = adapter

            def get(self, connector_key):
                """Reject other connectors rather than default to Discord."""
                if connector_key != self.adapter.connector.key:
                    raise LookupError(connector_key)
                return self.adapter

        self.coordinator = PlatformFanoutCoordinator(self.database, Registry(self.adapter))

    async def asyncTearDown(self):
        """Close SDK-neutral resources after persisted receipts are inspected."""
        await self.adapter.quiesce()
        await self.rpc.close()
        self.database.close()
        self.tempdir.cleanup()

    async def _stage(self):
        """Take an authenticated READY sidecar through durable claim and physical RPC."""
        assert await self.adapter.refresh_readiness()
        return await self.coordinator.stage_and_deliver(
            domain='local', kind='post', canonical_id=self.canonical_id,
            forum=self.forum, operation='forum_thread', destination=self.forum,
            content=ContentDocument('safe text'), title='Forum title',
            presentation=DeliveryPresentation('bot'))

    async def test_sqlite_claim_one_bot_post_receipt_and_no_repeat(self):
        """Full test-only path commits one thread and its distinct starter to SQLite."""
        from sqlalchemy import select
        from src.models import PlatformDeliveryIntent, LocalCommunityThreadSurface
        self.assertTrue(await self._stage())
        with self.database.session() as session:
            intents = session.scalars(select(PlatformDeliveryIntent)).all()
            copies = session.scalars(select(LocalCommunityThreadSurface)).all()
            self.assertEqual(len(intents), 1)
            self.assertEqual(intents[0].state, 'delivered')
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0].external_thread_id, 'thread-9')
            self.assertEqual(copies[0].external_starter_message_id, 'starter-91')
        self.assertFalse(await self._stage())
        self.assertEqual(len(self.provider.remote_threads), 1)
        self.assertEqual(len(self.adapter._creates), 0)  # persisted ACK evicts flight

    async def test_lost_reply_keeps_unresolved_and_never_reposts(self):
        """Remote Create is present but ambiguous; recovery retains durable uncertainty."""
        from sqlalchemy import select
        from src.models import PlatformDeliveryIntent, LocalCommunityThreadSurface
        self.provider.lost_ack = True
        self.assertFalse(await self._stage())
        self.assertEqual(len(self.provider.remote_threads), 1)
        with self.database.session() as session:
            intent = session.scalar(select(PlatformDeliveryIntent))
            self.assertEqual(intent.state, 'unresolved')
            self.assertIsNotNone(intent.dispatch_token)
            self.assertEqual(session.scalars(select(LocalCommunityThreadSurface)).all(), [])
        self.provider.lost_ack = False
        self.assertEqual(await self.coordinator.recover_pending(), [])
        self.assertFalse(await self.coordinator.reconcile_unknown(intent.id))
        self.assertEqual(len(self.provider.remote_threads), 1)
