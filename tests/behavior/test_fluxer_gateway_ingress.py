"""SQLite-backed Fluxer forum ingress remains nonpublishing until issue #13."""

from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import FastAPI

from src.internal_fluxer_api import create_internal_fluxer_router
import tempfile
import unittest
from pathlib import Path

from src.db import Database
from src.db.repositories.platform_surfaces import PlatformSurfaceRepository
from src.fluxer_ingress import FluxerInboundHandler
from src.models import (
    ChannelSurface, LocalCommunity, LocalCommunityThread,
    LocalCommunityThreadSurface, PlatformConnector,
)
from src.platform_types import (
    AuthorRef, ChannelRef, ContentDocument, MessageCreated,
    MessageDeleted, MessageRef, MessageUpdated, ThreadCreated,
    ThreadDeleted, ThreadRef, ThreadUpdated,
)


class FluxerForumObservationScenarios(unittest.IsolatedAsyncioTestCase):
    """One test-only forum claim never creates a canonical ActivityPub object."""

    async def asyncSetUp(self) -> None:
        """Seed a real isolated SQLite owner without cross-platform binding."""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Database(f"sqlite:///{Path(temporary.name) / 'bridge.db'}")
        self.addCleanup(self.database.close)
        self.database.migrate()
        with self.database.immediate_session() as session:
            connector = PlatformSurfaceRepository.add_connector(
                session, connector_key="fx-one", platform="fluxer", instance_origin="https://one.test",
            )
            community = LocalCommunity(
                slug="fx-test", display_name="Fluxer test",
                actor_url="https://bridge.test/actors/communities/fx-test",
                inbox_url="https://bridge.test/c/inbox", outbox_url="https://bridge.test/c/outbox",
                followers_url="https://bridge.test/c/followers",
                public_key_pem="test", private_key_pem="test", status="active",
            )
            session.add(community)
            session.flush()
            surface = ChannelSurface(
                connector_id=connector.id, external_channel_id="forum-1",
                external_guild_id="guild-one", kind="forum", local_community_id=community.id,
            )
            session.add(surface)
            session.flush()
            # Test-only physical receipt: it does not authorize any Fluxer human AP actor.
            canonical = LocalCommunityThread(
                local_community_id=community.id, ap_activity_id="https://bridge.test/a/old",
                ap_object_id="https://bridge.test/o/old", direction="outbound",
                origin_kind="discord",
            )
            session.add(canonical)
            session.flush()
            session.add(LocalCommunityThreadSurface(
                connector_id=connector.id, channel_surface_id=surface.id,
                local_community_thread_id=canonical.id,
                external_thread_id="thread-1", external_starter_message_id="starter-1",
                role="host",
            ))
        self.handler = FluxerInboundHandler(self.database, "fx-one")
        self.forum = ChannelRef("fx-one", "forum-1", "forum", "guild-one")
        self.thread = ThreadRef(self.forum, "thread-1")
        self.person = AuthorRef("fx-one", "human-1", "human", "Anonymous")

    async def test_all_six_events_with_claim_never_publish_ap(self) -> None:
        """Transport observation must not mint canonical AP or Discord identities."""
        events = (
            ThreadCreated("fx-one", self.thread, "topic", self.person,
                          MessageRef("fx-one", "thread-1", "starter-1"), True),
            ThreadUpdated("fx-one", self.thread, "renamed"),
            ThreadDeleted("fx-one", self.thread),
            MessageCreated("fx-one", MessageRef("fx-one", "thread-1", "reply-2"),
                           self.person, ContentDocument("body"), parent_channel=self.forum),
            MessageUpdated("fx-one", MessageRef("fx-one", "thread-1", "starter-1")),
            MessageDeleted("fx-one", MessageRef("fx-one", "thread-1", "starter-1")),
        )
        with self.assertLogs("src.fluxer_ingress", level="INFO") as logged:
            for event in events:
                with self.subTest(kind=type(event).__name__):
                    await self.handler.accept_event(event)
        self.assertTrue(any("community_binding_not_active" in line for line in logged.output))
        with self.database.session() as session:
            # Only the explicitly seeded preexisting canonical row is present.
            self.assertEqual(session.query(LocalCommunityThread).count(), 1)

    async def test_id_only_unknown_copy_and_foreign_connector_fail_closed(self) -> None:
        """No parent forum is required to parse an ID-only edit/delete."""
        for event in (
            MessageUpdated("fx-one", MessageRef("fx-one", "thread-unknown", "m")),
            MessageDeleted("fx-one", MessageRef("fx-one", "thread-unknown", "m")),
        ):
            await self.handler.accept_event(event)
        wrong_guild = MessageDeleted(
            "fx-one", MessageRef("fx-one", "thread-1", "starter-1"),
            external_guild_id="guild-foreign",
        )
        self.assertFalse(self.handler._claimed_forum(wrong_guild))
        foreign = MessageDeleted("fx-two", MessageRef("fx-two", "thread-1", "starter-1"))
        with self.assertRaisesRegex(ValueError, "wrong Fluxer connector"):
            await self.handler.accept_event(foreign)
        with self.database.session() as session:
            self.assertEqual(session.query(LocalCommunityThread).count(), 1)

    async def test_quiesce_joins_admitted_sink_and_rejects_late(self) -> None:
        """A paused HTTP-owned callback must finish before the database closes."""
        entered = asyncio.Event()
        resume = asyncio.Event()
        original = self.handler.handle_platform_event

        async def slow_sink(event):
            """Hold one accepted request at the repository boundary."""
            entered.set()
            await resume.wait()
            await original(event)

        self.handler.handle_platform_event = slow_sink
        event = MessageDeleted("fx-one", MessageRef("fx-one", "thread-1", "starter-1"))
        accepted = asyncio.create_task(self.handler.accept_event(event))
        await entered.wait()
        draining = asyncio.create_task(self.handler.quiesce())
        await asyncio.sleep(0)
        self.assertFalse(draining.done())
        with self.assertRaisesRegex(RuntimeError, "quiescing"):
            await self.handler.accept_event(event)
        resume.set()
        await accepted
        await draining

    async def test_authenticated_http_accepts_id_only_update_delete(self) -> None:
        """End-to-end private ASGI route admits valid partial events without parent."""
        app = FastAPI()
        app.include_router(create_internal_fluxer_router({"fx-one": (self.handler, "test-secret")}))
        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": "Bearer test-secret", "X-Fluxer-Connector": "fx-one"}
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for kind in ("MessageUpdated", "MessageDeleted"):
                payload = {
                    "type": kind, "connector_key": "fx-one",
                    "message": {"connector_key": "fx-one", "external_container_id": "thread-1",
                                "external_message_id": "starter-1"},
                }
                if kind == "MessageUpdated":
                    payload["content"] = None
                response = await client.post("/internal/fluxer/events", headers=headers, json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), {"accepted": True})

    async def test_node_gateway_wire_is_admitted_and_still_cannot_publish_ap(self) -> None:
        """A real Node subprocess feeds authenticated HTTP, then owned SQLite gate."""
        import subprocess

        payload = {
            "id": "thread-1", "parentId": "forum-1", "guildId": "guild-one",
            "type": 11, "name": "Topic",
        }
        script = """
          import {normalizeSdkEvent} from './fluxer-connector/src/normalization.ts';
          let incoming=''; for await (const chunk of process.stdin) incoming+=chunk;
          process.stdout.write(JSON.stringify(normalizeSdkEvent(
            'fx-one','threadCreate',[JSON.parse(incoming)],
            {receivedAt:'2026-10-27T11:00:00.000Z'})));
        """
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            ['node', '--experimental-strip-types', '--input-type=module', '-e', script],
            cwd=root, input=json.dumps(payload), text=True, capture_output=True,
            check=True, timeout=10,
        )
        app = FastAPI()
        app.include_router(create_internal_fluxer_router({"fx-one": (self.handler, "test-secret")}))
        headers = {"Authorization": "Bearer test-secret", "X-Fluxer-Connector": "fx-one"}
        with self.assertLogs("src.fluxer_ingress", level="INFO") as logged:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                response = await client.post("/internal/fluxer/events", headers=headers,
                                             content=result.stdout)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), {"accepted": True})
        self.assertTrue(any("community_binding_not_active" in line for line in logged.output))
        with self.database.session() as session:
            # A confirmed physical owner still cannot mint a Fluxer AP author.
            self.assertEqual(session.query(LocalCommunityThread).count(), 1)

    async def test_real_node_http_forwarder_reaches_python_sqlite_ingress(self) -> None:
        """Gateway DTO traverses Node TCP → FastAPI → owned SQLite → AP gate."""
        import socket
        import uvicorn

        app = FastAPI()
        app.include_router(create_internal_fluxer_router({"fx-one": (self.handler, "test-secret") }))
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(32)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, lifespan="off", log_level="critical",
        ))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(200):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(server.started)
            script = """
              import {normalizeSdkEvent} from './fluxer-connector/src/normalization.ts';
              import {GatewayEventForwarder} from './fluxer-connector/src/event_delivery.ts';
              let incoming=''; for await (const chunk of process.stdin) incoming+=chunk;
              const wire=normalizeSdkEvent('fx-one','threadCreate',[JSON.parse(incoming)],
                {receivedAt:'2026-10-27T11:00:00.000Z'});
              const forwarder=new GatewayEventForwarder({connectorKey:'fx-one',
                pythonIngressUrl:process.env.TEST_INGRESS,transportSecret:'test-secret'});
              try {await forwarder.forward(wire);} finally {await forwarder.close();}
            """
            payload = {"id": "thread-1", "parentId": "forum-1",
                       "guildId": "guild-one", "type": 11, "name": "Topic",
                       }
            import os
            root = Path(__file__).resolve().parents[2]
            env = {**os.environ,
                   "TEST_INGRESS": f"http://127.0.0.1:{port}/internal/fluxer/events"}
            with self.assertLogs("src.fluxer_ingress", level="INFO") as logged:
                process = await asyncio.create_subprocess_exec(
                    'node', '--experimental-strip-types', '--input-type=module',
                    '-e', script, cwd=root, env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(json.dumps(payload).encode('utf8')), timeout=10,
                )
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertEqual(stdout, b'')
            self.assertTrue(any("community_binding_not_active" in x for x in logged.output))
            with self.database.session() as session:
                self.assertEqual(session.query(LocalCommunityThread).count(), 1)
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=10)
            listener.close()

    async def test_private_http_rejects_bad_auth_schema_and_late_calls(self) -> None:
        """Malformed or unauthenticated events do not cross the sink boundary."""
        app = FastAPI()
        app.include_router(create_internal_fluxer_router({"fx-one": (self.handler, "test-secret")}))
        transport = httpx.ASGITransport(app=app)
        correct = {"Authorization": "Bearer test-secret", "X-Fluxer-Connector": "fx-one"}
        valid = {
            "type": "MessageDeleted", "connector_key": "fx-one",
            "message": {"connector_key": "fx-one", "external_container_id": "thread-1",
                        "external_message_id": "starter-1"},
        }
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            self.assertEqual((await client.post("/internal/fluxer/events", json=valid)).status_code, 401)
            self.assertEqual((await client.post("/internal/fluxer/events", json=valid,
                                               headers={**correct, "Authorization": "Bearer wrong"})).status_code, 401)
            self.assertEqual((await client.post("/internal/fluxer/events", json={**valid, "unexpected": 1},
                                               headers=correct)).status_code, 422)
            self.assertEqual((await client.post("/internal/fluxer/events", json={**valid, "connector_key": "fx-other"},
                                               headers=correct)).status_code, 422)
            self.assertEqual((await client.post("/internal/fluxer/events", json={**valid, "message": {"connector_key":"fx-one", "external_container_id":"thread-1"}},
                                               headers=correct)).status_code, 422)
            self.assertEqual((await client.post("/internal/fluxer/events", content=b'{"type":"MessageDeleted","type":"MessageUpdated"}',
                                               headers=correct)).status_code, 422)
            self.assertEqual((await client.post("/internal/fluxer/events", content=b'X' * 262145,
                                               headers=correct)).status_code, 413)
            await self.handler.quiesce()
            self.assertEqual((await client.post("/internal/fluxer/events", json=valid,
                                               headers=correct)).status_code, 503)


if __name__ == "__main__":
    unittest.main()
