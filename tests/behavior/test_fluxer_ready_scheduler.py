"""Real authenticated Node RPC health controls Fluxer scheduler pre-claim admission."""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

import httpx

from src.fluxer_adapter import FluxerAdapter
from src.fluxer_connector_config import FluxerConnectorConfig
from src.fluxer_rpc_client import FluxerRpcClient
from src.content_sync.platform_delivery_scheduler import PlatformPhysicalDeliveryScheduler


class FluxerReadySchedulerScenarios(unittest.IsolatedAsyncioTestCase):
    """Verify READY, offline and session rebind without modifying durable copies."""

    async def asyncSetUp(self) -> None:
        """Create one private RPC fake; no network credentials or Discord SDK."""
        self.health = {"state": "ready", "bot_id": "authenticated-bot",
                       "connector_key": "fluxer-one", "instance_origin": "https://fx.test"}
        self.seen = []
        def provider(request: httpx.Request) -> httpx.Response:
            """Model a single authenticated Node SDK session health endpoint."""
            assert request.headers["authorization"] == "Bearer shared-secret"
            payload = json.loads(request.content)
            self.seen.append(payload["operation"])
            return httpx.Response(200, json={"ok": True, "connector_key": "fluxer-one",
                                             "result": dict(self.health)})
        self.rpc = FluxerRpcClient(FluxerConnectorConfig(
            "fluxer-one", "https://fx.test", "http://127.0.0.1:8769", "shared-secret"),
            transport=httpx.MockTransport(provider))
        self.adapter = FluxerAdapter(connector_key="fluxer-one",
                                     instance_origin="https://fx.test", rpc=self.rpc)

    async def asyncTearDown(self) -> None:
        """Join adapter before the underlying authenticated HTTP transport closes."""
        await self.adapter.quiesce()
        await self.rpc.close()

    async def test_ready_then_offline_and_unsafe_bot_rebind(self) -> None:
        """Readiness needs exact current bot and origin, not cached config."""
        self.assertFalse(self.adapter.dispatch_ready())
        self.assertTrue(await self.adapter.refresh_readiness())
        self.assertTrue(self.adapter.dispatch_ready())
        self.health["state"] = "reconnecting"
        self.assertFalse(await self.adapter.refresh_readiness())
        self.assertFalse(self.adapter.dispatch_ready())
        self.health["state"] = "ready"
        self.health["bot_id"] = "new-bot"
        self.assertFalse(await self.adapter.refresh_readiness())
        self.health["bot_id"] = "authenticated-bot"
        self.health["instance_origin"] = "https://different.test"
        self.assertFalse(await self.adapter.refresh_readiness())
        self.health["instance_origin"] = "https://fx.test"
        self.health["gateway_gap"] = True
        self.assertFalse(await self.adapter.refresh_readiness())
        self.assertEqual(self.seen, ["health"] * 5)

    async def test_offline_node_skips_only_its_lane(self) -> None:
        """A queued Fluxer is not claimed and a ready Discord lane still drains."""
        progressed = asyncio.Event()
        self.health["state"] = "offline"
        invoked = []
        class Registry:
            """Select exactly the fake Node adapter or one independent Discord."""
            def get(inner, key):
                """Refuse undiscovered connectors without a fallback."""
                if key == "fluxer-one":
                    return self.adapter
                if key == "discord-primary":
                    return SimpleNamespace(dispatch_ready=lambda: True)
                raise LookupError(key)
        class Fanout:
            """Record observable recovery entry rather than predicting a claim."""
            registry = Registry()
            async def recover_pending(inner, *, connector_key):
                """Fail if the offline connector enters durable recovery."""
                invoked.append(("create", connector_key))
                if connector_key == "discord-primary":
                    progressed.set()
        class Mutations:
            """Observe the scheduler's independently admitted mutation lane."""
            async def drain(inner, *, connector_key, limit):
                """No offline Fluxer mutation may reach durable claim selection."""
                invoked.append(("mutation", connector_key))
                assert limit == 1
                return 0
        scheduler = PlatformPhysicalDeliveryScheduler(
            connector_keys=("fluxer-one", "discord-primary"),
            fanout=Fanout(), mutations=Mutations(), poll_seconds=30)
        scheduler.start()
        try:
            await asyncio.wait_for(progressed.wait(), 3)
            self.assertNotIn(("create", "fluxer-one"), invoked)
            self.assertNotIn(("mutation", "fluxer-one"), invoked)
            self.health["state"] = "ready"
            scheduler.wake("fluxer-one")
            for _ in range(100):
                if ("create", "fluxer-one") in invoked:
                    break
                await asyncio.sleep(0)
            self.assertIn(("create", "fluxer-one"), invoked)
        finally:
            await scheduler.stop()


if __name__ == "__main__":
    unittest.main()
