"""Real-process SQLite runtime ownership and deployment transition scenarios."""

from __future__ import annotations

import multiprocessing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.runtime_ownership import acquire_runtime_lifetime_lease, runtime_lifetime_lock_path
from src.profile_media import FileLock


def _racing_owner(db_url: str, start, stopped, outcomes) -> None:
    """Hold a real kernel flock until the controlling process releases the barrier."""
    try:
        with acquire_runtime_lifetime_lease(db_url):
            outcomes.put("held")
            start.set()
            stopped.wait(10)
    except BaseException as error:
        outcomes.put(type(error).__name__)


class SingleRuntimeOwnershipTests(unittest.TestCase):
    """Assert one owner from pre-recovery through deployment lock transitions."""

    def setUp(self) -> None:
        """Allocate separate canonical database and deployment lock paths."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.db_path = self.root / "bridge.db"
        self.db_path.touch()

    def test_same_db_path_and_alias_have_single_kernel_owner(self) -> None:
        """Competing processes cannot acquire via dot path or symlink alias."""
        alias = self.root / "alias.db"
        alias.symlink_to(self.db_path)
        ctx = multiprocessing.get_context("spawn")
        entered, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
        process = ctx.Process(target=_racing_owner, args=(f"sqlite:///{self.db_path}", entered, release, results))
        process.start()
        try:
            self.assertTrue(entered.wait(10))
            self.assertEqual(results.get(timeout=5), "held")
            self.assertEqual(runtime_lifetime_lock_path(f"sqlite:///{alias}"),
                             runtime_lifetime_lock_path(f"sqlite:///{self.db_path}"))
            with self.assertRaisesRegex(RuntimeError, "runtime already active"):
                acquire_runtime_lifetime_lease(f"sqlite:///{alias}")
            # The deployment shared handoff is not an ownership permission.
            with FileLock(self.root / "deployment.lock").acquire(exclusive=False, timeout_seconds=0):
                with self.assertRaisesRegex(RuntimeError, "runtime already active"):
                    acquire_runtime_lifetime_lease(f"sqlite:///{self.db_path}")
        finally:
            release.set()
            process.join(10)
            if process.is_alive():
                process.kill()
                process.join(5)
        self.assertEqual(process.exitcode, 0)
        with acquire_runtime_lifetime_lease(f"sqlite:///{self.db_path}"):
            pass

    def test_independent_databases_and_invalid_urls(self) -> None:
        """Other files run independently; memory/URI URLs never omit the lease."""
        second = self.root / "second.db"
        with acquire_runtime_lifetime_lease(f"sqlite:///{self.db_path}"):
            with acquire_runtime_lifetime_lease(f"sqlite:///{second}"):
                self.assertNotEqual(runtime_lifetime_lock_path(f"sqlite:///{second}"),
                                    runtime_lifetime_lock_path(f"sqlite:///{self.db_path}"))
        for url in ("sqlite://", "sqlite:///:memory:", "sqlite:///file:shared?mode=memory&cache=shared", "postgresql://host/db"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                runtime_lifetime_lock_path(url)

    def test_failed_build_does_not_leak_runtime_or_deployment_lock(self) -> None:
        """Startup errors must unwind lifetime and deployment locks in order."""
        settings = SimpleNamespace(
            database_url=f"sqlite:///{self.db_path}",
            deployment_state_lock_path=str(self.root / "deployment.lock"),
            profile_media_root=str(self.root / "media"),
            profile_media_lock_timeout_seconds=0,
        )
        try:
            from src.app import build_runtime
        except ModuleNotFoundError as error:
            if error.name == "discord":
                self.skipTest("discord.py absent: full runtime build integration unavailable")
            raise
        with patch("src.app.recover_incomplete_restore", side_effect=RuntimeError("cutover failed")):
            with self.assertRaisesRegex(RuntimeError, "cutover failed"):
                build_runtime(settings)
        # Failing recovery must not leave either descriptor locked.
        with acquire_runtime_lifetime_lease(settings.database_url):
            with FileLock(self.root / "deployment.lock").acquire(exclusive=True, timeout_seconds=0):
                pass

    def test_abrupt_process_death_releases_kernel_lock(self) -> None:
        """A SIGKILL cannot retain the lease or require manual lockfile removal."""
        ctx = multiprocessing.get_context("spawn")
        entered, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
        process = ctx.Process(target=_racing_owner, args=(f"sqlite:///{self.db_path}", entered, release, results))
        process.start()
        try:
            self.assertTrue(entered.wait(10))
            self.assertEqual(results.get(timeout=5), "held")
            process.kill()
            process.join(10)
            self.assertFalse(process.is_alive())
            with acquire_runtime_lifetime_lease(f"sqlite:///{self.db_path}"):
                pass
        finally:
            if process.is_alive():
                process.kill()
                process.join(5)


if __name__ == "__main__":
    unittest.main()
