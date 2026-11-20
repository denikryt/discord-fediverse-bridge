"""Behavioral runtime composition with one Fluxer sidecar and real SQLite ingress."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path

import httpx
import pytest

from src.app import build_runtime
from src.config import Settings
from src.http_api import create_http_app


def _settings(root: Path, *, enabled: bool) -> Settings:
    """Give the real runtime independent database/lease paths and optional Node RPC."""
    raw = {
        "BRIDGE_PLATFORM": "fluxer" if enabled else "discord",
        "PUBLIC_BASE_URL": "https://bridge.example",
        "FEDIFY_SHARED_SECRET": "test-fedify-bearer",
    }
    if not enabled:
        raw.update(DISCORD_TOKEN="test-discord-token", BRIDGE_SUPER_ADMIN_USER_IDS="1234")
    if enabled:
        raw["FLUXER_CONNECTORS_JSON"] = json.dumps([{
            "connector_key": "fluxer-one", "origin": "https://fluxer.example",
            "rpc_url": "http://127.0.0.1:8769", "transport_secret": "test-internal-bearer",
        }])
    validated = Settings.model_validate(raw)
    config = validated.model_dump()
    config.update(database_url=f"sqlite:///{root / 'bridge.db'}",
                  profile_media_root=str(root / 'media'),
                  deployment_state_lock_path=str(root / 'deployment.lock'),
                  profile_media_lock_path=str(root / 'media.lock'))
    return Settings.model_construct(**config)


@pytest.mark.asyncio
async def test_opt_in_registers_exact_adapter_and_authenticated_http_sink(tmp_path: Path) -> None:
    """Gateway HTTP admission reaches the one real SQLite sink, not Discord routing."""
    runtime = build_runtime(_settings(tmp_path, enabled=True))
    try:
        adapter = runtime.adapter_registry.get("fluxer-one")
        assert runtime.fluxer_adapter is adapter
        assert adapter.connector.instance_origin == "https://fluxer.example"
        assert runtime.fluxer_ingress is not None
        assert runtime.physical_delivery_scheduler.connector_keys == ("fluxer-one",)
        assert runtime.bot is None and runtime.discord_adapter is None
        assert runtime.discord_oauth_client is None
        assert runtime.personal_feed_discord_delivery_worker is None
        assert runtime.physical_mutation_coordinator is None
        assert not adapter.dispatch_ready()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_http_app(runtime)),
                                     base_url="http://bridge") as client:
            payload = {"type": "MessageDeleted", "connector_key": "fluxer-one",
                       "message": {"connector_key": "fluxer-one",
                                   "external_container_id": "thread-1",
                                   "external_message_id": "reply-1"}}
            denied = await client.post("/internal/fluxer/events", json=payload)
            assert denied.status_code == 401
            admitted = await client.post("/internal/fluxer/events", json=payload, headers={
                "Authorization": "Bearer test-internal-bearer", "X-Fluxer-Connector": "fluxer-one",
            })
            assert admitted.status_code == 200 and admitted.json() == {"accepted": True}
            foreign = await client.post("/internal/fluxer/events", json={**payload,
                "connector_key": "discord-primary"}, headers={
                    "Authorization": "Bearer test-internal-bearer", "X-Fluxer-Connector": "fluxer-one",
                })
            assert foreign.status_code == 422
            await runtime.fluxer_ingress.quiesce()
            late = await client.post("/internal/fluxer/events", json=payload, headers={
                "Authorization": "Bearer test-internal-bearer", "X-Fluxer-Connector": "fluxer-one",
            })
            assert late.status_code == 503
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_sidecar_readiness_pins_authenticated_bot_and_rejects_restart_rebind(tmp_path: Path) -> None:
    """Only current authenticated READY identity authorizes recovery admission."""
    runtime = build_runtime(_settings(tmp_path, enabled=True))
    adapter = runtime.fluxer_adapter
    assert adapter is not None
    current = {"state": "ready", "bot_id": "bot-1", "connector_key": "fluxer-one",
               "instance_origin": "https://fluxer.example"}

    def provider(request: httpx.Request) -> httpx.Response:
        """Model authenticated Node health without a second REST transport."""
        assert request.headers["Authorization"] == "Bearer test-internal-bearer"
        data = json.loads(request.content)
        assert data["operation"] == "health"
        return httpx.Response(200, json={"ok": True, "connector_key": "fluxer-one",
                                         "result": current.copy()})

    try:
        await runtime.fluxer_rpc._http.aclose()
        runtime.fluxer_rpc._http = httpx.AsyncClient(
            transport=httpx.MockTransport(provider), base_url="http://127.0.0.1:8769")
        assert not adapter.dispatch_ready()
        assert await adapter.refresh_readiness() is True
        assert adapter.dispatch_ready()
        current["state"] = "reconnecting"
        assert await adapter.refresh_readiness() is False
        assert not adapter.dispatch_ready()
        current.update(state="ready", bot_id="bot-replaced")
        assert await adapter.refresh_readiness() is False
        assert not adapter.dispatch_ready()
        current.update(bot_id="bot-1", instance_origin="https://foreign.example")
        assert await adapter.refresh_readiness() is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.skipif(importlib.util.find_spec('discord') is None, reason='Discord-only test requires discord.py')
async def test_disabled_fluxer_has_no_adapter_lane_or_route(tmp_path: Path) -> None:
    """Default startup leaves Discord-only composition and private routes unchanged."""
    runtime = build_runtime(_settings(tmp_path, enabled=False))
    try:
        assert runtime.fluxer_adapter is None and runtime.fluxer_rpc is None
        assert runtime.fluxer_ingress is None
        assert runtime.physical_delivery_scheduler.connector_keys == ("discord-primary",)
        with pytest.raises(LookupError):
            runtime.adapter_registry.get("fluxer-one")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_http_app(runtime)),
                                     base_url="http://bridge") as client:
            response = await client.post("/internal/fluxer/events", json={})
            assert response.status_code == 404
    finally:
        await runtime.close()
