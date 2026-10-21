"""Connector-keyed registry of externally owned platform adapter instances.

This process-local registry neither owns SDK clients nor changes bridge database
claims. Runtime composition will register live adapters only from Discord adapter integration.
"""

from __future__ import annotations

from collections.abc import Iterable

from .platform_types import ChannelRef, ConnectorRef
from .runtime_ports import PlatformAdapterPort


class PlatformAdapterRegistry:
    """Select the exact configured connector with no implicit Discord fallback."""

    def __init__(self, connectors: Iterable[ConnectorRef]) -> None:
        """Freeze allowed persisted connector snapshots for this registry lifetime."""
        self._allowed: dict[str, ConnectorRef] = {}
        self._adapters: dict[str, PlatformAdapterPort] = {}
        for connector in connectors:
            if not isinstance(connector, ConnectorRef):
                raise ValueError('registry requires ConnectorRef metadata')
            if connector.key in self._allowed:
                raise ValueError(f'duplicate configured connector: {connector.key}')
            self._allowed[connector.key] = connector

    def register(self, adapter: PlatformAdapterPort) -> None:
        """Bind one exact connector; never replace a registered SDK client."""
        if not isinstance(adapter, PlatformAdapterPort):
            raise TypeError('adapter does not implement PlatformAdapterPort')
        identity = adapter.connector
        if not isinstance(identity, ConnectorRef):
            raise ValueError('adapter connector metadata is invalid')
        expected = self._allowed.get(identity.key)
        if expected is None or expected != identity:
            # Compare origin/platform, not only external channel IDs or key.
            raise ValueError(f'unconfigured or mismatched connector: {identity.key}')
        if identity.key in self._adapters:
            raise ValueError(f'duplicate adapter registration: {identity.key}')
        self._adapters[identity.key] = adapter

    def get(self, connector_key: str) -> PlatformAdapterPort:
        """Find a live registered adapter or fail with no default connector."""
        try:
            return self._adapters[connector_key]
        except (KeyError, TypeError) as exc:
            raise LookupError(f'platform adapter not registered: {connector_key!r}') from exc

    def get_for_channel(self, channel: ChannelRef) -> PlatformAdapterPort:
        """Select solely from channel's connector; do not infer domain claims."""
        if not isinstance(channel, ChannelRef):
            raise ValueError('channel lookup requires ChannelRef')
        return self.get(channel.connector_key)
