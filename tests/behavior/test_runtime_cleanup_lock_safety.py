"""SQLite startup/shutdown failures keep both kernel locks until safe teardown."""

from __future__ import annotations

import asyncio
import gc
import multiprocessing
import traceback
from pathlib import Path
from unittest.mock import patch

import pytest

from src.config import Settings
from src.db import Database
from src.profile_media import FileLock
from src.runtime_ownership import acquire_runtime_lifetime_lease


def _settings(root: Path) -> Settings:
    """Use real file-backed SQLite and isolated operator state paths."""
    return Settings.model_construct(
        discord_token="test-token", database_url=f"sqlite:///{root / 'bridge.db'}",
        public_base_url="https://bridge.example", bridge_gateway_url="http://127.0.0.1:3000",
        fedify_shared_secret="test-secret", bridge_super_admin_user_ids="1234567890",
        profile_media_root=str(root / "media"),
        deployment_state_lock_path=str(root / "deployment.lock"),
        profile_media_lock_path=str(root / "media.lock"),
    )


def _failed_owner(root: str, phase: str, channel) -> None:
    """Keep failed real DB teardown alive while the parent probes both locks."""
    from src import app

    settings = _settings(Path(root))
    opened = []
    original_migrate = Database.migrate

    class UnclosedDatabase(Database):
        """Leave the actual SQLite engine alive when disposal fails."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            opened.append(self)

        def close(self):
            """Inject the failure before real connection disposal."""
            raise RuntimeError("SQLite close refused")

        def migrate(self, *args, **kwargs):
            """Cover composition before and after a complete Runtime exists."""
            original_migrate(self, *args, **kwargs)
            if phase == "partial":
                raise ValueError("startup rejected")

    try:
        with patch.object(app, "Database", UnclosedDatabase), patch.object(
            app, "_validate_runtime_graph", side_effect=ValueError("startup rejected")
        ):
            app.build_runtime(settings)
    except BaseException as error:
        # Force collection: keeping only an exception traceback is insufficient
        # protection for an application that catches and discards startup errors.
        diagnostic = "".join(traceback.format_exception(error))
        fatal = isinstance(error, SystemExit)
    else:
        raise AssertionError("startup unexpectedly succeeded")
    gc.collect()
    with opened[0].engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT 1").scalar() == 1
    try:
        app.build_runtime(settings)
    except BaseException as error:
        restart_blocked = isinstance(error, SystemExit)
    else:
        restart_blocked = False
    channel.send((fatal, restart_blocked, diagnostic))
    assert channel.recv() == "exit"
    # A fatal cleanup failure terminates this process; the kernel then releases
    # both descriptors, rather than an error handler resuming service.
    raise SystemExit(1)


@pytest.mark.parametrize("phase", ["partial", "complete"])
def test_failed_startup_blocks_second_runtime_and_restore_until_process_exit(tmp_path, phase):
    """Another process cannot restart or restore while SQLite is unclosed."""
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = ctx.Process(target=_failed_owner, args=(str(tmp_path), phase, child))
    process.start()
    child.close()
    try:
        assert parent.poll(30), "owner did not report startup failure"
        fatal, restart_blocked, diagnostic = parent.recv()
        assert fatal and restart_blocked, diagnostic
        assert "startup rejected" in diagnostic
        assert "SQLite close refused" in diagnostic
        with pytest.raises(RuntimeError, match="runtime already active"):
            acquire_runtime_lifetime_lease(_settings(tmp_path).database_url)
        # Restore uses an exclusive deployment lease; the failed owner must
        # retain its shared lease even after the original exception is collected.
        with pytest.raises(TimeoutError):
            FileLock(tmp_path / "deployment.lock").acquire(exclusive=True, timeout_seconds=0)
        parent.send("exit")
        process.join(10)
        assert process.exitcode == 1
        with acquire_runtime_lifetime_lease(_settings(tmp_path).database_url):
            with FileLock(tmp_path / "deployment.lock").acquire(exclusive=True, timeout_seconds=0):
                pass
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()


def test_confirmed_shutdown_retry_releases_locks_only_after_database_closes(tmp_path):
    """An explicit safe retry clears quarantine after genuine SQLite disposal."""
    from src.app import build_runtime

    runtime = build_runtime(_settings(tmp_path))
    close = runtime.database.close
    try:
        with patch.object(runtime.database, "close", side_effect=RuntimeError("SQLite close refused")):
            with pytest.raises(SystemExit):
                asyncio.run(runtime.close())
        with pytest.raises(TimeoutError):
            FileLock(tmp_path / "deployment.lock").acquire(exclusive=True, timeout_seconds=0)
        with pytest.raises(RuntimeError, match="runtime already active"):
            acquire_runtime_lifetime_lease(_settings(tmp_path).database_url)
    finally:
        # The explicit retry uses the real engine disposal, not a mock success.
        runtime.database.close = close
        asyncio.run(runtime.close())
    with acquire_runtime_lifetime_lease(_settings(tmp_path).database_url):
        with FileLock(tmp_path / "deployment.lock").acquire(exclusive=True, timeout_seconds=0):
            pass
