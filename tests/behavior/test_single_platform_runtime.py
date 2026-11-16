"""Exclusive-runtime acceptance scenarios for the issue #12 implementation sequence.

These tests intentionally describe the final observable contract before the
configuration, database and runtime branches are wired in CP02–CP06.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import signal
import socket
import shutil
import tempfile
import unittest
from unittest import mock
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


class FluxerProcessLifetimeContracts(unittest.IsolatedAsyncioTestCase):
    """Exercise the real Python HTTP process without a Discord bot or Node SDK."""

    async def test_failed_http_service_releases_active_runtime_leases(self) -> None:
        """A failed HTTP task must close RPC/workers/DB and permit exact restart."""
        from src.app import build_runtime, main

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = config(Path(directory.name))

        class FailingHttpServer:
            """Fail at the outer server boundary before accepting callbacks."""
            started = False
            should_exit = False

            def __init__(self, *_args, **_kwargs) -> None:
                """Mirror only the lifecycle fields inspected by the app owner."""

            async def serve(self) -> None:
                """Model a listening failure with no unresolved ingress tasks."""
                raise RuntimeError('synthetic HTTP failure')

        with mock.patch('src.app.Settings', return_value=settings), mock.patch(
            'src.app._RuntimeHttpServer', FailingHttpServer
        ):
            with self.assertRaises(BaseExceptionGroup):
                await main()
        # A leaked runtime-lifetime or deployment lease would fail this build.
        runtime = build_runtime(settings)
        await runtime.close()

    async def test_fluxer_http_stays_healthy_offline_and_handles_sigterm(self) -> None:
        """Gateway absence cannot stop HTTP; signal must release SQLite leases."""
        import httpx
        from src.app import main, build_runtime

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        settings = config(root).model_copy(update={
            "internal_http_host": "127.0.0.1", "internal_http_port": port,
            "log_level": "ERROR",
        })
        with mock.patch('src.app.Settings', return_value=settings):
            process = asyncio.create_task(main())
            try:
                async with httpx.AsyncClient(timeout=0.5) as client:
                    for attempt in range(100):
                        try:
                            response = await client.get(f'http://127.0.0.1:{port}/healthz')
                        except (httpx.ConnectError, httpx.ReadTimeout):
                            await asyncio.sleep(0.025)
                            continue
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()['status'], 'ok')
                        break
                    else:
                        self.fail('Fluxer-only HTTP did not become healthy without Node')
                # main() owns SIGTERM only while both the listener and leases
                # are active. Its handler shuts admission before asynchronous drain.
                signal.raise_signal(signal.SIGTERM)
                await asyncio.wait_for(process, timeout=15)
                # No dangling SQLite owner may prevent a second exact boot.
                restarted = build_runtime(settings)
                await restarted.close()
            finally:
                if not process.done():
                    process.cancel()
                    try:
                        await process
                    except asyncio.CancelledError:
                        pass


class FluxerHttpAndFederationBoundaryContracts(unittest.IsolatedAsyncioTestCase):
    """Unsupported Discord registration/user federation must fail before mutation."""

    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        from src.app import build_runtime
        self.runtime = build_runtime(config(Path(self.directory.name)))
        self.addAsyncCleanup(self.runtime.close)

    async def test_registration_and_oauth_cannot_create_discord_state(self) -> None:
        import httpx
        from sqlalchemy import select, func
        from src.http_api import create_http_app
        from src.models import RegistrationSession

        app = create_http_app(self.runtime)
        routes = {getattr(route, 'path', None) for route in app.routes}
        self.assertNotIn('/auth/discord/start', routes)
        self.assertNotIn('/auth/discord/callback', routes)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://local-test') as client:
            for path, method in (('/register', 'GET'),
                                 ('/register/complete', 'POST'),
                                 ('/register/success', 'GET')):
                with self.subTest(path=path):
                    response = await client.request(method, path, data={'username':'alice'})
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()['detail'], 'registration_unavailable')
                    self.assertNotIn('set-cookie', response.headers)
            for path in ('/auth/discord/start',
                         '/auth/discord/callback?code=abc&state=xyz'):
                with self.subTest(path=path):
                    self.assertEqual((await client.get(path)).status_code, 404)
            self.assertEqual((await client.get('/healthz')).status_code, 200)
            self.assertEqual((await client.get('/')).status_code, 200)
            dashboard = await client.get('/dashboard/data')
            self.assertEqual(dashboard.status_code, 200, dashboard.text)
        with self.runtime.database.session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(RegistrationSession)), 0)

    async def test_user_follow_undo_and_personal_feed_do_not_touch_discord_owners(self) -> None:
        from datetime import datetime, timezone
        from sqlalchemy import select, func
        from src.activitypub_models import LocalFollowRequestEvent, LocalUnfollowRequestEvent
        from src.activitypub_handlers import dispatch_activitypub_event
        from src.inbound_activity_outcomes import InboundActivityOutcome
        from src.models import UserActorFollower

        now = datetime.now(timezone.utc)
        actor = 'https://remote.example/users/alice'
        target = 'https://bridge.test/actors/users/unknown'
        follow = LocalFollowRequestEvent(
            event_type='local.follow_requested', delivery_id='follow-one', occurred_at=now,
            target_actor_kind='user', target_actor_id=target, actor_id=actor,
            object={'follow_activity_id': 'https://remote.example/follows/1',
                    'remote_inbox_url':'https://remote.example/users/alice/inbox'},
        )
        undo = LocalUnfollowRequestEvent(
            event_type='local.unfollow_requested', delivery_id='undo-one', occurred_at=now,
            target_actor_kind='user', target_actor_id=target, actor_id=actor,
            object={'follow_activity_id': 'https://remote.example/follows/1'},
        )
        with mock.patch.object(self.runtime.user_actor_follow_service, 'handle_follow',
                               side_effect=AssertionError('unverified Fluxer user Follow invoked')) as accept, \
             mock.patch.object(self.runtime.user_actor_follow_service, 'handle_unfollow',
                               side_effect=AssertionError('unverified Fluxer user Undo invoked')) as revoke:
            for event in (follow, undo):
                with self.subTest(event=event.event_type):
                    result = await dispatch_activitypub_event(event, self.runtime)
                    self.assertEqual(result.status, 'skipped')
                    self.assertEqual(result.outcome, InboundActivityOutcome.IGNORED_UNKNOWN_FOLLOW)
            accept.assert_not_called()
            revoke.assert_not_called()
        # A signed but mismapped personal-feed comment must not dereference
        # the absent Discord-only conversation runtime.
        from src.activitypub_models import ActivityPubEvent
        personal = ActivityPubEvent(
            event_type='comment.created', delivery_id='personal-one', occurred_at=now,
            actor_id=actor, target={'kind': 'personal_feed',
                                    'root_object_id': 'https://bridge.test/notes/old-root'},
            object={
                'ap_id': 'https://remote.example/notes/reply', 'kind': 'comment',
                'published_at': now, 'author_name': 'alice', 'parent_ap_id': 'https://bridge.test/notes/old-root',
                'attributed_to_actor_id': actor,
            },
        )
        skipped = await dispatch_activitypub_event(personal, self.runtime)
        self.assertEqual(skipped.outcome, InboundActivityOutcome.IGNORED_UNMAPPED_CONTEXT)
        self.assertEqual(skipped.status, 'skipped')
        with self.runtime.database.session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(UserActorFollower)), 0)


if __name__ == "__main__":
    unittest.main()
