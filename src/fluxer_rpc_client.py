"""Bounded, authenticated Python-to-Fluxer private RPC transport.

The private JSON envelope is bridge-owned, not a Fluxer REST route. SDK-specific
routing and bearer bot credentials remain within the Node connector process.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .fluxer_connector_config import FluxerConnectorConfig
from .platform_types import PlatformOperationError


class FluxerRpcClient:
    """Call one explicitly configured sidecar; never retry uncertain writes."""

    _READS = frozenset({
        'fetch_channel', 'fetch_thread', 'fetch_message', 'check_permissions',
        'capabilities', 'reconcile_create', 'health',
    })
    _WRITES = frozenset({
        'create_forum_thread', 'create_reply_thread', 'send_message',
        'edit_message', 'delete_message',
    })
    _BODY_LIMIT = 256 * 1024
    _REPLY_LIMIT = 1024 * 1024
    _ERRORS = frozenset({
        'not_found', 'forbidden', 'unsupported', 'invalid_request',
        'rate_limited', 'unavailable', 'unknown_delivery',
    })

    def __init__(self, config: FluxerConnectorConfig, *,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        """Bind a single connector to one private endpoint and token."""
        self._config = config
        self._http = httpx.AsyncClient(
            base_url=config.rpc_url, transport=transport, follow_redirects=False,
            trust_env=False, timeout=httpx.Timeout(10.0, connect=3.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._open = True
        self._active: set[asyncio.Task[Any]] = set()
        self._closing: asyncio.Task[None] | None = None

    async def call(self, operation: str, args: dict[str, Any], *,
                   connector_key: str | None = None) -> dict[str, Any]:
        """Validate operation and connector before any network/SDK access."""
        if not self._open:
            raise PlatformOperationError('unavailable', 'Fluxer private RPC is closed')
        if operation not in self._READS | self._WRITES:
            raise PlatformOperationError('unsupported', 'unsupported Fluxer RPC operation')
        if connector_key is not None and connector_key != self._config.connector_key:
            raise PlatformOperationError('invalid_request', 'Fluxer RPC connector mismatch')
        if not isinstance(args, dict) or any(not isinstance(key, str) for key in args):
            raise PlatformOperationError('invalid_request', 'invalid Fluxer RPC arguments')
        request = {'connector_key': self._config.connector_key,
                   'operation': operation, 'args': args}
        try:
            encoded = json.dumps(request, allow_nan=False, separators=(',', ':')).encode('utf-8')
        except (TypeError, ValueError, UnicodeError) as exc:
            raise PlatformOperationError('invalid_request', 'invalid Fluxer RPC arguments') from exc
        if len(encoded) > self._BODY_LIMIT:
            raise PlatformOperationError('invalid_request', 'Fluxer RPC request exceeds size limit')
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError('Fluxer RPC requires asyncio task ownership')
        self._active.add(task)
        try:
            return await self._request(operation, encoded)
        finally:
            self._active.discard(task)

    async def _request(self, operation: str, body: bytes) -> dict[str, Any]:
        """Send at most once; a failed write ACK never authorizes replay."""
        write = operation in self._WRITES
        try:
            async with self._http.stream(
                'POST', '/rpc', content=body,
                headers={
                    'Authorization': f'Bearer {self._config.transport_secret}',
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
            ) as response:
                # The transport must never follow a redirect with a bearer secret.
                if 300 <= response.status_code < 400:
                    raise PlatformOperationError(
                        'unknown_delivery' if write else 'invalid_request',
                        'private RPC redirected',
                    )
                if response.status_code >= 500:
                    raise PlatformOperationError(
                        'unknown_delivery' if write else 'unavailable',
                        'Fluxer private RPC unavailable',
                    )
                if response.status_code == 429:
                    raise PlatformOperationError('rate_limited', 'Fluxer private RPC rate limited')
                if response.status_code not in (200, 400, 401, 403, 404, 422):
                    raise PlatformOperationError(
                        'unknown_delivery' if write else 'unavailable',
                        'Fluxer private RPC unrecognized response',
                    )
                data = bytearray()
                async for part in response.aiter_bytes():
                    if len(data) + len(part) > self._REPLY_LIMIT:
                        raise PlatformOperationError(
                            'unknown_delivery' if write and response.status_code == 200
                            else 'invalid_request',
                            'Fluxer RPC response exceeds size limit',
                        )
                    data.extend(part)
                try:
                    envelope = json.loads(data)
                except (ValueError, UnicodeError) as exc:
                    raise PlatformOperationError(
                        'unknown_delivery' if write and response.status_code == 200
                        else 'invalid_request', 'invalid Fluxer private RPC response',
                    ) from exc
                if not isinstance(envelope, dict) or (
                    envelope.get('connector_key') != self._config.connector_key
                    or type(envelope.get('ok')) is not bool
                ):
                    raise PlatformOperationError(
                        'unknown_delivery' if write and response.status_code == 200
                        else 'invalid_request', 'Fluxer private RPC response scope mismatch',
                    )
                if response.status_code == 200 and envelope['ok'] is True:
                    result = envelope.get('result')
                    if not isinstance(result, dict):
                        raise PlatformOperationError(
                            'unknown_delivery' if write else 'invalid_request',
                            'Fluxer private RPC result missing',
                        )
                    return result
                if envelope['ok'] is True:
                    raise PlatformOperationError('invalid_request', 'Fluxer private RPC inconsistent status')
                error = envelope.get('error')
                reason = error.get('reason') if isinstance(error, dict) else None
                # Only explicit, scoped errors can be treated as definite refusals.
                if reason not in self._ERRORS:
                    raise PlatformOperationError(
                        'unknown_delivery' if write else 'unavailable',
                        'Fluxer private RPC unverified refusal',
                    )
                if response.status_code == 200 and write:
                    raise PlatformOperationError('unknown_delivery', 'Fluxer private RPC rejected write')
                raise PlatformOperationError(reason, 'Fluxer private RPC rejected request')
        except httpx.RequestError as exc:
            raise PlatformOperationError(
                'unknown_delivery' if write else 'unavailable',
                'Fluxer private RPC transport unavailable',
            ) from exc

    async def close(self) -> None:
        """Close admission, await accepted calls, then dispose the HTTP client."""
        self._open = False
        task = asyncio.current_task()
        pending = tuple(current for current in self._active if current is not task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self._http.aclose()
