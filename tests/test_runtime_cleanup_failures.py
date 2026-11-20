"""Real Runtime teardown preserves SQLite protections and every failure cause."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bridge_test_support.runtime import runtime_settings
from src.app import build_runtime
from src.profile_media import FileLock
from src.runtime_cleanup import UnsafeRuntimeShutdown
from src.runtime_ownership import acquire_runtime_lifetime_lease


def _operations(runtime):
    """List the production cleanup boundaries in their observable order."""
    return [
        ("ingress", runtime.discord_adapter, "drain_ingress"),
        ("scheduler", runtime.physical_delivery_scheduler, "stop"),
        ("dispatch", runtime.physical_dispatch_gate, "drain"),
        ("feed", runtime.personal_feed_discord_delivery_worker, "stop"),
        ("user", runtime.user_actor_delivery_worker, "stop"),
        ("io", runtime.discord_adapter, "quiesce"),
        ("oauth", runtime.discord_oauth_client, "close"),
        ("fedify", runtime.fedify_gateway, "close"),
        ("database", runtime.database, "close"),
        ("deployment", runtime.deployment_state_lease, "close"),
        ("lifetime", runtime.runtime_lifetime_lease, "close"),
    ]


def _observe(monkeypatch, runtime, steps, failures):
    """Wrap actual teardown; inject failures before the selected owner closes."""
    for name, owner, method in _operations(runtime):
        original = getattr(owner, method)
        if asyncio.iscoroutinefunction(original):
            async def run(original=original, name=name):
                """Record async ownership joins while preserving genuine cleanup."""
                steps.append(name)
                if name in failures:
                    raise failures[name]
                await original()
        else:
            def run(original=original, name=name):
                """Keep real DB disposal and lock release on successful paths."""
                steps.append(name)
                if name in failures:
                    raise failures[name]
                original()
        monkeypatch.setattr(owner, method, run)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["ingress", "scheduler", "dispatch", "feed", "user", "io", "oauth", "fedify", "database"])
async def test_runtime_close_attempts_resources_and_retains_both_locks(tmp_path, monkeypatch, failure):
    """One failed owner cannot skip other resources or authorize restart/restore."""
    runtime = build_runtime(runtime_settings(tmp_path))
    steps = []
    try:
        with monkeypatch.context() as injected:
            _observe(injected, runtime, steps, {failure: RuntimeError(f"{failure} failure")})
            with pytest.raises(UnsafeRuntimeShutdown) as raised:
                await runtime.close()
        assert [str(e) for e in raised.value.__cause__.exceptions] == [f"{failure} failure"]
        assert steps == [name for name, _, _ in _operations(runtime)][:-2]
        with pytest.raises(RuntimeError, match="runtime already active"):
            acquire_runtime_lifetime_lease(runtime.settings.database_url)
        with pytest.raises(TimeoutError):
            FileLock(Path(runtime.settings.deployment_state_lock_path)).acquire(
                exclusive=True, timeout_seconds=0)
    finally:
        # Genuine successful teardown, rather than clearing a test-only global,
        # is the only way this process may build another runtime after failure.
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_close_normal_completion_releases_every_owner(tmp_path, monkeypatch):
    """Successful shutdown releases deployment then lifetime after real disposal."""
    runtime = build_runtime(runtime_settings(tmp_path))
    steps = []
    with monkeypatch.context() as observed:
        _observe(observed, runtime, steps, {})
        await runtime.close()
    assert steps == [name for name, _, _ in _operations(runtime)]
    with acquire_runtime_lifetime_lease(runtime.settings.database_url):
        with FileLock(Path(runtime.settings.deployment_state_lock_path)).acquire(
            exclusive=True, timeout_seconds=0):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_runtime_close_retains_primary_and_secondary_errors(tmp_path, monkeypatch, cancel):
    """A later DB error cannot overwrite the worker error or cancellation."""
    runtime = build_runtime(runtime_settings(tmp_path))
    first = asyncio.CancelledError() if cancel else RuntimeError("first feed failure")
    last = RuntimeError("database failure")
    try:
        with monkeypatch.context() as injected:
            _observe(injected, runtime, [], {"feed": first, "database": last})
            with pytest.raises(UnsafeRuntimeShutdown) as raised:
                await runtime.close()
        assert raised.value.__cause__.exceptions == (first, last)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_failed_deployment_unlock_cannot_release_lifetime_lease(tmp_path, monkeypatch):
    """Even after successful SQLite disposal, deployment release is ordered."""
    runtime = build_runtime(runtime_settings(tmp_path))
    try:
        with monkeypatch.context() as injected:
            _observe(injected, runtime, [], {"deployment": RuntimeError("unlock refused")})
            with pytest.raises(UnsafeRuntimeShutdown):
                await runtime.close()
        with pytest.raises(RuntimeError, match="runtime already active"):
            acquire_runtime_lifetime_lease(runtime.settings.database_url)
    finally:
        await runtime.close()
