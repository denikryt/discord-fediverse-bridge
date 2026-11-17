"""SQLite v1 and operator-registration scenarios for Fluxer physical identity."""

from __future__ import annotations

import shutil
import multiprocessing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.db import Database
from src.db.repositories.platform_surfaces import PlatformSurfaceRepository
from src.models import PlatformConnector


def _insert_origin_from_process(path: str, key: str, barrier, results) -> None:
    """Attempt one real cross-process SQLite write after a shared barrier."""
    connection = sqlite3.connect(path, timeout=10)
    try:
        barrier.wait(timeout=10)
        connection.execute(
            "INSERT INTO platform_connectors (connector_key, platform, instance_origin) "
            "VALUES (?, 'fluxer', 'https://example.test')",
            (key,),
        )
        connection.commit()
        results.put("committed")
    except sqlite3.IntegrityError:
        connection.rollback()
        results.put("rejected")
    finally:
        connection.close()


class FluxerOriginV1Tests(unittest.TestCase):
    """Observe connector persistence, conflicts, and strict schema validation."""

    def setUp(self) -> None:
        """Create a distinct file-backed SQLite database for every scenario."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "bridge.sqlite"

    def _open(self, *, legacy: bool = False) -> Database:
        """Bootstrap the real schema or upgrade a copied, released-v0 fixture."""
        if legacy:
            shutil.copyfile(
                Path(__file__).parent / "fixtures/release_v0_1_0_original.sqlite",
                self.path,
            )
        db = Database(f"sqlite:///{self.path}")
        self.addCleanup(db.close)
        db.migrate()
        return db

    def _check_index(self, db: Database) -> None:
        """Assert that the actual on-disk schema enforces the v1 index."""
        with db.engine.connect() as connection:
            names = {row[1] for row in connection.exec_driver_sql(
                "PRAGMA index_list('platform_connectors')"
            )}
            self.assertIn("ux_platform_connectors_fluxer_origin", names)
            index_sql = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_platform_connectors_fluxer_origin'"
            ).scalar_one()
            self.assertIn("UNIQUE INDEX", index_sql.upper())
            self.assertIn("platform = 'fluxer'", index_sql)
            self.assertEqual(connection.exec_driver_sql("PRAGMA user_version").scalar_one(), 1)
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_key_check").all(), [])
            self.assertEqual(connection.exec_driver_sql("PRAGMA integrity_check").scalar_one(), "ok")

    def test_empty_and_released_v0_both_have_unique_fluxer_index(self) -> None:
        """Fresh and migrated v1 targets must have identical origin constraints."""
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.path.unlink(missing_ok=True)
                db = self._open(legacy=legacy)
                self._check_index(db)
                db.migrate()  # Strict validation must accept the unchanged current v1.
                db.close()

    def test_identical_saved_origin_fails_inside_sqlite_not_just_repository(self) -> None:
        """Conflicting direct SQL writes cannot bypass platform origin ownership."""
        db = self._open()
        with db.immediate_session() as session:
            first = PlatformSurfaceRepository.add_connector(
                session, connector_key="first", platform="fluxer", instance_origin="https://EXAMPLE.test:443"
            )
            self.assertEqual(first.instance_origin, "https://example.test")
        with self.assertRaises(IntegrityError):
            with db.engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO platform_connectors (connector_key, platform, instance_origin) "
                    "VALUES ('second', 'fluxer', 'https://example.test')"
                )
        # The failed transaction must leave the first physical identity intact.
        with db.session() as session:
            self.assertEqual(
                [(row.connector_key, row.instance_origin) for row in session.scalars(
                    select(PlatformConnector).where(PlatformConnector.platform == "fluxer")
                )],
                [("first", "https://example.test")],
            )

    def test_two_processes_cannot_commit_same_saved_origin(self) -> None:
        """Separate processes can race, but only one owns the saved origin."""
        db = self._open()
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        results = context.Queue()
        workers = [
            context.Process(
                target=_insert_origin_from_process,
                args=(str(self.path), key, barrier, results),
            )
            for key in ("a", "b")
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
            self.assertFalse(worker.is_alive(), "origin registration process hung")
            self.assertEqual(worker.exitcode, 0)
        self.assertCountEqual([results.get(timeout=2) for _ in workers], ["committed", "rejected"])
        with db.session() as session:
            self.assertEqual(session.query(PlatformConnector).filter_by(platform="fluxer").count(), 1)

    def test_normalization_idempotency_and_immutable_connector_keys(self) -> None:
        """Same key/origin is safe to reuse; wrong bindings fail before writes."""
        db = self._open()
        with db.immediate_session() as session:
            first = PlatformSurfaceRepository.add_connector(
                session, connector_key="fx", platform="fluxer", instance_origin="https://EXAMPLE.test"
            )
            again = PlatformSurfaceRepository.add_connector(
                session, connector_key="fx", platform="fluxer", instance_origin="https://example.test."
            )
            self.assertEqual(first.id, again.id)
            with self.assertRaises(ValueError):
                PlatformSurfaceRepository.add_connector(
                    session, connector_key="fx", platform="fluxer", instance_origin="https://another.test"
                )
            with self.assertRaises(ValueError):
                PlatformSurfaceRepository.add_connector(
                    session, connector_key="fx", platform="discord"
                )
        with db.session() as session:
            self.assertEqual(session.query(PlatformConnector).count(), 2)  # Discord + Fluxer.

    def test_canonical_url_equivalences_and_bad_origin_rejection(self) -> None:
        """Transport identity canonicalization must be exact and non-guessing."""
        canonical = PlatformSurfaceRepository.canonical_origin
        for original, expected in (
            ("https://EXAMPLE.test", "https://example.test"),
            ("https://example.test:443", "https://example.test"),
            ("https://example.test.", "https://example.test"),
            ("http://example.test:80", "http://example.test"),
            ("https://[2001:0DB8::1]:443", "https://[2001:db8::1]"),
            ("https://127.0.0.1:443", "https://127.0.0.1"),
            ("https://bücher.test", "https://xn--bcher-kva.test"),
        ):
            with self.subTest(original=original):
                self.assertEqual(canonical(original), expected)
        for invalid in (
            "https://example.test./", "https://u:p@example.test", "https://example.test/path",
            "https://example.test?", "https://example.test#", "https://example.test:99999",
            "https://example.test:bogus", "https://example..test", "https://example.test ",
            "https://[fe80::1%eth0]", "https://", "https://-bad.test",
            # WHATWG URL would rewrite these noncanonical IPv4 authority forms;
            # SQLite must reject rather than save a different Node SDK origin.
            "https://127.1", "https://0x7f000001", "https://0177.0.0.1",
            "https://2130706433", "https://127.0.0.01",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                canonical(invalid)

    def test_development_v1_missing_required_index_is_not_silently_upgraded(self) -> None:
        """Strict v1 validates its target instead of repairing an obsolete dev DB."""
        db = self._open()
        with db.engine.begin() as connection:
            connection.exec_driver_sql("DROP INDEX ux_platform_connectors_fluxer_origin")
        with self.assertRaises(Exception):
            db.migrate()
        with db.engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA user_version").scalar_one(), 1)
            self.assertIsNone(connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name='ux_platform_connectors_fluxer_origin'"
            ).scalar_one_or_none())


if __name__ == "__main__":
    unittest.main()
