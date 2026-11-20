"""Physical sends from immediate and recovery paths share one Fluxer slot."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from src.content_sync.platform_delivery_scheduler import ConnectorDispatchCapacity
from src.content_sync.platform_fanout import PlatformFanoutCoordinator
from src.fluxer_adapter import FluxerAdapter
from src.fluxer_connector_config import FluxerConnectorConfig
from src.fluxer_rpc_client import FluxerRpcClient


class FluxerDirectCapacityScenarios(unittest.IsolatedAsyncioTestCase):
    """Direct coordinators cannot bypass the configured connector capacity."""

    async def asyncSetUp(self) -> None:
        """Configure one transport without starting or impersonating an SDK bot."""
        config = FluxerConnectorConfig('fx-one', 'https://fx.test',
                                       'http://127.0.0.1:8769', 'ipc-bearer')
        self.rpc = FluxerRpcClient(config)
        self.adapter = FluxerAdapter(connector_key='fx-one',
                                     instance_origin='https://fx.test', rpc=self.rpc)
        self.gate = ConnectorDispatchCapacity(('discord-primary', 'fx-one'))

    async def asyncTearDown(self) -> None:
        """Do not let a test fixture retain socket or physical operation owners."""
        await self.adapter.quiesce()
        await self.rpc.close()

    async def test_direct_coordinator_inherits_one_runtime_gate(self) -> None:
        """A busy Fluxer slot blocks another Fluxer sender, not Discord."""
        self.adapter.bind_physical_dispatch_gate(self.gate)
        direct = PlatformFanoutCoordinator.from_adapter(SimpleNamespace(), self.adapter)
        self.assertIs(direct.dispatch_gate, self.gate)
        with self.assertRaisesRegex(RuntimeError, 'already bound'):
            self.adapter.bind_physical_dispatch_gate(self.gate)
        other_admitted = asyncio.Event()
        async with self.gate.slot('fx-one'):
            self.assertEqual(self.gate.active_count('fx-one'), 1)
            # Two independent slot acquisitions cannot overlap on the same
            # connector. The Discord slot remains independently available.
            waiting = asyncio.create_task(self._another_slot(other_admitted))
            async with self.gate.slot('discord-primary'):
                self.assertEqual(self.gate.active_count('discord-primary'), 1)
                self.assertFalse(other_admitted.is_set())
        await asyncio.wait_for(waiting, 3)
        self.assertTrue(other_admitted.is_set())
        self.assertEqual(self.gate.active_count('fx-one'), 0)

    async def _another_slot(self, admitted: asyncio.Event) -> None:
        """Observe the second physical caller only after the first leaves."""
        async with self.gate.slot('fx-one'):
            admitted.set()

    async def test_invalid_gate_cannot_rebind_the_adapter(self) -> None:
        """An unrelated runtime's capacity does not admit this connector."""
        other = ConnectorDispatchCapacity(('discord-primary',))
        with self.assertRaisesRegex(RuntimeError, 'wrong connector'):
            self.adapter.bind_physical_dispatch_gate(other)
        self.assertIsNone(self.adapter._physical_dispatch_gate)


if __name__ == '__main__':
    unittest.main()
