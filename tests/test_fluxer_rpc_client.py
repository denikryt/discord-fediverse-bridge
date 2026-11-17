"""Contract tests for the private authenticated Python-to-Fluxer sidecar RPC."""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from src.fluxer_connector_config import FluxerConnectorConfig
from src.fluxer_rpc_client import FluxerRpcClient
from src.platform_types import PlatformOperationError


class FluxerRpcContractScenarios(unittest.IsolatedAsyncioTestCase):
    """Validate request/response scope at the actual local HTTP transport edge."""

    def _config(self) -> FluxerConnectorConfig:
        """Use a nonpublic, explicit test-sidecar binding without real secrets."""
        return FluxerConnectorConfig(
            connector_key='fluxer-test', origin='https://fluxer.example',
            rpc_url='http://127.0.0.1:8721',
            transport_secret='secret-transport-token',
        )

    def _client(self, handler):
        """Inject a stateful local provider rather than replacing RPC logic."""
        transport = httpx.MockTransport(handler)
        return FluxerRpcClient(self._config(), transport=transport)

    async def test_direct_rpc_rejects_unsafe_bearer_before_http_header(self) -> None:
        """Even programmatic configs cannot bypass the authenticated header contract."""
        for secret in ('1234567é', 'abc\rdefgh', 'abc defgh'):
            with self.subTest(secret=repr(secret)), self.assertRaises(ValueError):
                FluxerRpcClient(FluxerConnectorConfig(
                    'fluxer-test', 'https://fluxer.example',
                    'http://127.0.0.1:8721', secret,
                ), transport=httpx.MockTransport(lambda req: httpx.Response(200)))

    async def test_authenticated_scoped_read_at_exact_private_path(self) -> None:
        """The sidecar sees one authorized connector-scoped payload, no bot token."""
        calls = []
        def server(request):
            """Record the provider-visible request before reporting a channel."""
            calls.append((str(request.url), dict(request.headers), request.content))
            return httpx.Response(200, json={
                'connector_key': 'fluxer-test', 'ok': True,
                'result': {'id': 'opaque-channel-1', 'kind': 'forum'},
            })
        client = self._client(server)
        try:
            result = await client.call('fetch_channel', {'id': 'opaque-channel-1'})
        finally:
            await client.close()
        self.assertEqual(result, {'id': 'opaque-channel-1', 'kind': 'forum'})
        self.assertEqual(len(calls), 1)
        url, headers, content = calls[0]
        self.assertEqual(url, 'http://127.0.0.1:8721/rpc')
        self.assertEqual(headers['authorization'], 'Bearer secret-transport-token')
        self.assertLess(len(content), 262144)
        self.assertEqual(json.loads(content)['connector_key'], 'fluxer-test')
        self.assertNotIn(b'bot_token', content)
        self.assertFalse(hasattr(self._config(), 'bot_token'))

    async def test_wrong_connector_and_unknown_method_rejected_without_io(self) -> None:
        """No fallback to another instance or arbitrary private operation name."""
        calls = []
        def server(request):
            """Reject any network call made for invalid local admission."""
            calls.append(request)
            return httpx.Response(200)
        client = self._client(server)
        try:
            for operation, args, connector in (
                ('unknown_route', {}, None),
                ('fetch_message', {}, 'another-connector'),
                ('create_forum_thread', {'unexpected': object()}, None),
            ):
                with self.subTest(operation=operation), self.assertRaises(PlatformOperationError):
                    await client.call(operation, args, connector_key=connector)
            self.assertEqual(calls, [])
        finally:
            await client.close()

    async def test_transport_failure_after_write_entry_is_unknown_without_retry(self) -> None:
        """A dropped RPC response cannot authorize another physical POST."""
        calls = []
        def server(request):
            """Simulate a Node process that committed a write then lost its ACK."""
            calls.append(request)
            raise httpx.ReadError('response lost', request=request)
        client = self._client(server)
        try:
            with self.assertRaises(PlatformOperationError) as captured:
                await client.call('create_forum_thread', {'forum_id': '11'})
            self.assertEqual(captured.exception.reason, 'unknown_delivery')
            self.assertEqual(len(calls), 1)
            self.assertNotIn('secret', str(captured.exception).lower())
        finally:
            await client.close()

    async def test_prewrite_denial_and_ambiguous_server_error_differ(self) -> None:
        """A definitive 403 is not the same as a possibly effected HTTP 503."""
        seen = []
        def server(request):
            """Return verified refusal or an untrusted ambiguous provider failure."""
            seen.append(request)
            if len(seen) == 1:
                return httpx.Response(403, json={
                    'connector_key': 'fluxer-test', 'ok': False,
                    'error': {'reason': 'forbidden'},
                })
            return httpx.Response(503, json={'error': 'temporary'})
        client = self._client(server)
        try:
            with self.assertRaises(PlatformOperationError) as first:
                await client.call('send_message', {'destination': 'f'})
            self.assertEqual(first.exception.reason, 'forbidden')
            with self.assertRaises(PlatformOperationError) as second:
                await client.call('send_message', {'destination': 'f'})
            self.assertEqual(second.exception.reason, 'unknown_delivery')
            self.assertEqual(len(seen), 2)
        finally:
            await client.close()

    async def test_malformed_or_oversized_reply_fails_closed(self) -> None:
        """A success code is insufficient without a matching scoped envelope."""
        responses = [
            httpx.Response(200, json={'connector_key': 'other', 'ok': True, 'result': {}}),
            httpx.Response(200, json={'connector_key': 'fluxer-test', 'ok': True}),
            httpx.Response(200, content=b'{' + b' ' * 1_100_000 + b'}'),
        ]
        def server(request):
            """Deliver the next invalid private provider response."""
            return responses.pop(0)
        client = self._client(server)
        try:
            for _ in range(3):
                with self.assertRaises(PlatformOperationError):
                    await client.call('fetch_thread', {'thread_id': 'opaque'})
        finally:
            await client.close()

    async def test_quiesce_joins_admitted_rpc_before_client_close(self) -> None:
        """An in-flight transport callback finishes before HTTP resources close."""
        entered = asyncio.Event()
        release = asyncio.Event()
        def server(request):
            """Return deterministic data for a held in-flight HTTP request."""
            return httpx.Response(200, json={'connector_key': 'fluxer-test',
                                             'ok': True, 'result': {}})
        client = self._client(server)
        original = client._request
        async def paused(*args, **kwargs):
            """Pause inside client ownership after RPC admission."""
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        client._request = paused
        accepted = asyncio.create_task(client.call('fetch_channel', {'id': '1'}))
        await entered.wait()
        shutdown = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        self.assertFalse(shutdown.done())
        with self.assertRaises(PlatformOperationError):
            await client.call('fetch_channel', {'id': '2'})
        release.set()
        self.assertEqual(await accepted, {})
        await shutdown


if __name__ == '__main__':
    unittest.main()
