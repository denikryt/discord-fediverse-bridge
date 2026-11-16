"""Exclusive-runtime acceptance scenarios for the issue #12 implementation sequence.

These tests intentionally describe the final observable contract before the
configuration, database and runtime branches are wired in CP02–CP06.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from src.config import Settings


CONNECTOR = {
    "connector_key": "fx-primary",
    "origin": "https://fluxer.test",
    "rpc_url": "http://127.0.0.1:8769",
    "transport_secret": "private-test-bearer",
}


def config(root: Path, *, platform: str = "fluxer", connector: dict | None = None) -> Settings:
    """Build one isolated deployment's validated settings without real credentials."""
    raw = {
        "BRIDGE_PLATFORM": platform,
        "PUBLIC_BASE_URL": "https://bridge.test",
        "FEDIFY_SHARED_SECRET": "test-gateway-secret",
    }
    if platform == "discord":
        raw.update(DISCORD_TOKEN="discord-test", BRIDGE_SUPER_ADMIN_USER_IDS="123")
    if connector is not None or platform == "fluxer":
        raw["FLUXER_CONNECTORS_JSON"] = json.dumps([connector or CONNECTOR])
    settings = Settings.model_validate(raw)
    return settings.model_copy(update={
        "database_url": f"sqlite:///{root / 'bridge.sqlite'}",
        "profile_media_root": str(root / "media"),
        "deployment_state_lock_path": str(root / "deployment.lock"),
        "profile_media_lock_path": str(root / "media.lock"),
    })


class ExclusiveConfigurationContracts(unittest.TestCase):
    """Selection and credential refusal must precede all runtime side effects."""

    def test_default_discord_and_explicit_fluxer_require_distinct_credentials(self) -> None:
        """The old default stays Discord while Fluxer requires no Discord secret."""
        default = Settings.model_validate({
            "DISCORD_TOKEN": "test", "BRIDGE_SUPER_ADMIN_USER_IDS": "123",
            "PUBLIC_BASE_URL": "https://bridge.test", "FEDIFY_SHARED_SECRET": "test",
        })
        self.assertEqual(default.bridge_platform, "discord")
        with tempfile.TemporaryDirectory() as directory:
            selected = config(Path(directory))
            self.assertEqual(selected.bridge_platform, "fluxer")
            self.assertIsNone(selected.discord_token)
            self.assertEqual(len(selected.fluxer_connectors), 1)

    def test_invalid_modes_and_mixed_platform_credentials_fail_before_io(self) -> None:
        """Malformed modes and simultaneous platform ownership never reach SQLite."""
        common = {"PUBLIC_BASE_URL": "https://bridge.test", "FEDIFY_SHARED_SECRET": "test"}
        discord = {"DISCORD_TOKEN": "test", "BRIDGE_SUPER_ADMIN_USER_IDS": "123"}
        for values in (
            {"BRIDGE_PLATFORM": "both", **discord},
            {"BRIDGE_PLATFORM": "fluxer", **discord, "FLUXER_CONNECTORS_JSON": json.dumps([CONNECTOR])},
            {"BRIDGE_PLATFORM": "fluxer", "FLUXER_CONNECTORS_JSON": "[]"},
            {"BRIDGE_PLATFORM": "discord", **discord, "FLUXER_CONNECTORS_JSON": json.dumps([CONNECTOR])},
            {"BRIDGE_PLATFORM": "discord", **discord, "FLUXER_CONNECTORS_JSON": "[]"},
            {"BRIDGE_PLATFORM": "discord", "BRIDGE_SUPER_ADMIN_USER_IDS": "123"},
            {"BRIDGE_PLATFORM": "fluxer", "FLUXER_CONNECTORS_JSON": json.dumps([CONNECTOR]),
             "DISCORD_OAUTH_CLIENT_ID": "unexpected-discord-oauth"},
            {"BRIDGE_PLATFORM": "fluxer", "FLUXER_CONNECTORS_JSON": json.dumps([CONNECTOR, CONNECTOR])},
        ):
            with self.subTest(values=list(values)):
                with self.assertRaises(ValueError):
                    Settings.model_validate({**common, **values})


class ExclusiveRuntimeContracts(unittest.TestCase):
    """Exercise real SQLite ownership and the process's composition root."""

    def setUp(self) -> None:
        """Use isolated state and leases to make shutdown/rejection observable."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_fluxer_first_boot_and_exact_restart_have_no_discord_owner(self) -> None:
        """Only one Fluxer adapter is owned, and exact restart preserves signing keys."""
        from src.app import build_runtime
        from src.models import BridgeActorKey
        from sqlalchemy import select

        settings = config(self.root)
        for run in range(2):
            runtime = build_runtime(settings)
            try:
                self.assertIsNone(runtime.bot)
                self.assertIsNone(runtime.discord_adapter)
                self.assertIsNone(runtime.discord_oauth_client)
                self.assertEqual(runtime.physical_delivery_scheduler.connector_keys, ("fx-primary",))
                with runtime.database.session() as session:
                    key = session.scalar(select(BridgeActorKey))
                    self.assertIsNotNone(key)
                    if run == 0:
                        self.actor_material = (key.actor_url, key.private_key_data)
                    else:
                        self.assertEqual((key.actor_url, key.private_key_data), self.actor_material)
            finally:
                asyncio.run(runtime.close())

    def test_fluxer_runtime_rejects_accidental_discord_owner(self) -> None:
        """An accidental second platform owner is refused at graph validation."""
        from src.app import _validate_runtime_graph, build_runtime

        runtime = build_runtime(config(self.root))
        try:
            self.assertIsNone(runtime.bot)
            runtime.bot = object()
            with self.assertRaisesRegex(RuntimeError, 'Discord services'):
                _validate_runtime_graph(runtime)
            runtime.bot = None
            _validate_runtime_graph(runtime)
        finally:
            asyncio.run(runtime.close())

    def test_initialized_discord_actor_key_cannot_be_adopted_by_fluxer(self) -> None:
        """An empty Discord deployment can still own a persistent AP signing key."""
        from src.actor_key_service import BridgeActorKeyBootstrap
        from src.db import Database
        from src.app import build_runtime

        old = config(self.root, platform="discord")
        db = Database(old.database_url)
        try:
            db.migrate()
            BridgeActorKeyBootstrap(database=db, settings=old).ensure()
        finally:
            db.close()
        database_file = self.root / "bridge.sqlite"
        before = hashlib.sha256(database_file.read_bytes()).digest()
        with self.assertRaisesRegex((RuntimeError, ValueError), "(mode|Discord|Fluxer|identity|key)"):
            build_runtime(config(self.root))
        self.assertEqual(hashlib.sha256(database_file.read_bytes()).digest(), before)

    def test_populated_released_v0_is_refused_without_migrating(self) -> None:
        """Fluxer cannot apply a committed legacy migration before mode refusal."""
        from src.app import build_runtime

        fixture = Path(__file__).resolve().parents[1] / "fixtures/release_v0_1_0_original.sqlite"
        destination = self.root / "bridge.sqlite"
        shutil.copy2(fixture, destination)
        before = hashlib.sha256(destination.read_bytes()).digest()
        with self.assertRaisesRegex((RuntimeError, ValueError), "(mode|Discord|legacy|Fluxer)"):
            build_runtime(config(self.root))
        self.assertEqual(hashlib.sha256(destination.read_bytes()).digest(), before)


class ExclusiveSchedulerContracts(unittest.IsolatedAsyncioTestCase):
    """Keep durable Create recovery without activating Fluxer canonical mutations."""

    async def test_no_mutation_worker_keeps_normal_recovery_running(self) -> None:
        """Fluxer fanout progresses without invoking a Discord-only formatter."""
        from src.content_sync.platform_delivery_scheduler import PlatformPhysicalDeliveryScheduler
        from types import SimpleNamespace

        recovered = asyncio.Event()
        events = []

        async def recover_pending(*, connector_key: str) -> None:
            """Observe the actual scheduler-selected connector and wake the test."""
            events.append(("create", connector_key))
            recovered.set()

        async def forbid_canonical_mutation(*, connector_key: str, limit: int) -> None:
            """Any mutation call would violate the disabled Fluxer lane contract."""
            self.fail("Unowned Fluxer canonical mutation was dispatched")

        adapter = SimpleNamespace(dispatch_ready=lambda: True)
        registry = SimpleNamespace(get=lambda key: adapter)
        fanout = SimpleNamespace(registry=registry, recover_pending=recover_pending)
        scheduler = PlatformPhysicalDeliveryScheduler(
            connector_keys=("fx-primary",), fanout=fanout, mutations=None,
            poll_seconds=0.05,
        )
        scheduler.start()
        try:
            await asyncio.wait_for(recovered.wait(), timeout=1)
            self.assertEqual(events[0], ("create", "fx-primary"))
        finally:
            await scheduler.stop()

    async def test_discord_lane_still_reaches_both_recovery_coordinators(self) -> None:
        """Existing Discord scheduler continues to recover Creates and edits."""
        from src.content_sync.platform_delivery_scheduler import PlatformPhysicalDeliveryScheduler
        from types import SimpleNamespace

        both = asyncio.Event()
        events = []

        async def recover_pending(*, connector_key: str) -> None:
            """Record the persistent Create lane's execution."""
            events.append("create")
            if "mutation" in events:
                both.set()

        async def drain(*, connector_key: str, limit: int) -> None:
            """Record the existing mutation coordinator's scheduled execution."""
            self.assertEqual((connector_key, limit), ("discord-primary", 1))
            events.append("mutation")
            if "create" in events:
                both.set()

        registry = SimpleNamespace(get=lambda key: SimpleNamespace(dispatch_ready=lambda: True))
        scheduler = PlatformPhysicalDeliveryScheduler(
            connector_keys=("discord-primary",),
            fanout=SimpleNamespace(registry=registry, recover_pending=recover_pending),
            mutations=SimpleNamespace(drain=drain), poll_seconds=0.05,
        )
        scheduler.start()
        try:
            await asyncio.wait_for(both.wait(), timeout=1)
            self.assertIn("create", events)
            self.assertIn("mutation", events)
        finally:
            await scheduler.stop()


if __name__ == "__main__":
    unittest.main()
