"""Real SIGTERM closes HTTP, Discord lifetime and SQLite before process exit."""
from __future__ import annotations

import asyncio
import multiprocessing
import os
from pathlib import Path
import signal
import pytest
import urllib.error
import urllib.request

from bridge_test_support.runtime import runtime_settings
from src.profile_media import FileLock
from src.runtime_ownership import acquire_runtime_lifetime_lease


def _running_bridge(root: str, mode: str, channel) -> None:
    """Run actual app.main and Uvicorn; replace only Discord's network start."""
    from src import app

    settings = runtime_settings(Path(root)).model_copy(update={
        'internal_http_host': '127.0.0.1', 'internal_http_port': 0,
    })
    runtime = app.build_runtime(settings)
    original_close = runtime.database.close

    def close_database():
        """Prove disposal happened before the process and its locks disappeared."""
        original_close()
        (Path(root) / 'database-closed').touch()

    runtime.database.close = close_database

    http_started = asyncio.Event()

    async def fake_discord_start(_token):
        """An authenticated, idle outer SDK connection survives until cancelled."""
        runtime.bot.bridge_ready.set()
        if mode == 'gateway_return':
            await http_started.wait()
            return
        await asyncio.Event().wait()

    runtime.bot.start = fake_discord_start
    app.Settings = lambda: settings
    app.build_runtime = lambda _settings: runtime
    original_startup = app.uvicorn.Server.startup

    async def observe_startup(server, *args, **kwargs):
        """Report the actual ephemeral listening socket after HTTP startup."""
        await original_startup(server, *args, **kwargs)
        channel.send(server.servers[0].sockets[0].getsockname()[1])
        http_started.set()
        if mode == 'http_return':
            server.should_exit = True

    app.uvicorn.Server.startup = observe_startup
    asyncio.run(app.main())
    channel.close()


@pytest.mark.parametrize('mode', ['sigterm', 'http_return', 'gateway_return'])
def test_service_stop_exits_after_database_cleanup_without_forced_kill(tmp_path, mode):
    """A real process signal drains an idle bridge instead of hanging or exiting early."""
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_running_bridge, args=(str(tmp_path), mode, child))
    process.start()
    child.close()
    try:
        assert parent.poll(30), 'HTTP server never started'
        port = parent.recv()
        if mode == 'sigterm':
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=5) as response:
                assert response.status == 200
            os.kill(process.pid, signal.SIGTERM)
        process.join(10)
        assert not process.is_alive(), 'HTTP stop left the Discord TaskGroup running'
        assert process.exitcode == 0, 'Signal bypassed orderly application cleanup'
        assert (tmp_path / 'database-closed').exists()
        with acquire_runtime_lifetime_lease(runtime_settings(tmp_path).database_url):
            with FileLock(tmp_path / 'deployment-state.lock').acquire(exclusive=True, timeout_seconds=0):
                pass
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()


def _unjoined_http_owner(root, channel):
    """Retain a failed external HTTP owner while the parent probes both leases."""
    from src.app import build_runtime
    from src.runtime_cleanup import UnsafeRuntimeShutdown

    runtime = build_runtime(runtime_settings(Path(root)))
    runtime.retain_unclosed_owner(object(), RuntimeError('HTTP callbacks did not join'))
    try:
        asyncio.run(runtime.close())
    except UnsafeRuntimeShutdown:
        channel.send('fatal')
        assert channel.recv() == 'exit'
        raise SystemExit(1)
    raise AssertionError('Unconfirmed external shutdown released the runtime')


def test_unjoined_http_callbacks_keep_restore_and_restart_excluded(tmp_path):
    """A DB close alone cannot authorize restore if external callbacks remain."""
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_unjoined_http_owner, args=(str(tmp_path), child))
    process.start()
    child.close()
    try:
        assert parent.poll(30)
        assert parent.recv() == 'fatal'
        with pytest.raises(RuntimeError, match='runtime already active'):
            acquire_runtime_lifetime_lease(runtime_settings(tmp_path).database_url)
        with pytest.raises(TimeoutError):
            FileLock(tmp_path / 'deployment-state.lock').acquire(exclusive=True, timeout_seconds=0)
        parent.send('exit')
        process.join(10)
        assert process.exitcode == 1
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
