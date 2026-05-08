"""Scenario tests for exporting local users into a standalone SQLite backup."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from src.db import Database
from src.user_identity_dump import main as dump_users_cli_main
from src.user_identity_dump import write_user_identity_backup


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite-backed database for user backup scenarios."""

    database = Database(f"sqlite:///{tmp_path / 'bridge-users.db'}")
    database.create_all()
    return database


def test_write_user_identity_backup_creates_restore_usable_sqlite_db(
    tmp_path: Path,
) -> None:
    """A user-only backup should preserve all identity/key fields in SQLite."""

    database = _database(tmp_path)
    database.create_user(
        discord_user_id="123456789",
        activitypub_username="alice",
        actor_url="https://bridge.example/users/alice",
        inbox_url="https://bridge.example/users/alice/inbox",
        outbox_url="https://bridge.example/users/alice/outbox",
        followers_url="https://bridge.example/users/alice/followers",
        public_key_pem="PUBLIC-KEY",
        private_key_pem="PRIVATE-KEY",
    )
    output_path = tmp_path / "users.db.backup"

    written_path = write_user_identity_backup(
        source_database_url=f"sqlite:///{tmp_path / 'bridge-users.db'}",
        output_path=output_path,
    )

    assert written_path == output_path
    with sqlite3.connect(output_path) as connection:
        row = connection.execute(
            """
            SELECT
                discord_user_id,
                activitypub_username,
                actor_url,
                public_key_pem,
                private_key_pem
            FROM users
            """
        ).fetchone()

    assert row == (
        "123456789",
        "alice",
        "https://bridge.example/users/alice",
        "PUBLIC-KEY",
        "PRIVATE-KEY",
    )


def test_dump_local_users_cli_writes_users_db_backup(tmp_path: Path) -> None:
    """The CLI should write a standalone SQLite file with the users table only."""

    database = _database(tmp_path)
    database.create_user(
        discord_user_id="999",
        activitypub_username="bob",
        actor_url="https://bridge.example/users/bob",
        inbox_url="https://bridge.example/users/bob/inbox",
        outbox_url="https://bridge.example/users/bob/outbox",
        followers_url="https://bridge.example/users/bob/followers",
        public_key_pem="PUBLIC-KEY-BOB",
        private_key_pem="PRIVATE-KEY-BOB",
    )
    output_path = tmp_path / "users.db.backup"

    result = subprocess.run(
        [
            sys.executable,
            "dev/dump_local_users.py",
            "--database-url",
            f"sqlite:///{tmp_path / 'bridge-users.db'}",
            "--output",
            str(output_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )

    # The CLI prints the written path so operators can compose this utility in
    # shell backup scripts without extra parsing.
    assert result.stdout.strip() == str(output_path)
    with sqlite3.connect(output_path) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        row = connection.execute(
            "SELECT activitypub_username, private_key_pem FROM users"
        ).fetchone()

    assert tables == [("users",)]
    assert row == ("bob", "PRIVATE-KEY-BOB")


def test_dump_local_users_entrypoint_writes_users_db_backup(tmp_path: Path) -> None:
    """The packaged CLI entry point should write the same backup artifact."""

    database = _database(tmp_path)
    database.create_user(
        discord_user_id="555",
        activitypub_username="carol",
        actor_url="https://bridge.example/users/carol",
        inbox_url="https://bridge.example/users/carol/inbox",
        outbox_url="https://bridge.example/users/carol/outbox",
        followers_url="https://bridge.example/users/carol/followers",
        public_key_pem="PUBLIC-KEY-CAROL",
        private_key_pem="PRIVATE-KEY-CAROL",
    )
    output_path = tmp_path / "entrypoint-users.db.backup"

    exit_code = dump_users_cli_main(
        [
            "--database-url",
            f"sqlite:///{tmp_path / 'bridge-users.db'}",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    with sqlite3.connect(output_path) as connection:
        row = connection.execute(
            "SELECT activitypub_username, private_key_pem FROM users"
        ).fetchone()

    assert row == ("carol", "PRIVATE-KEY-CAROL")


def test_dump_local_users_entrypoint_uses_default_paths(tmp_path: Path) -> None:
    """The CLI should use bridge.db and dev/users.db.backup when no args are given."""

    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.create_user(
        discord_user_id="777",
        activitypub_username="dave",
        actor_url="https://bridge.example/users/dave",
        inbox_url="https://bridge.example/users/dave/inbox",
        outbox_url="https://bridge.example/users/dave/outbox",
        followers_url="https://bridge.example/users/dave/followers",
        public_key_pem="PUBLIC-KEY-DAVE",
        private_key_pem="PRIVATE-KEY-DAVE",
    )
    output_path = tmp_path / "dev" / "users.db.backup"

    # The default CLI contract is intentionally simple so operators can run one
    # command from the project root without remembering any flags.
    original_cwd = Path.cwd()
    try:
        os.chdir(tmp_path)
        exit_code = dump_users_cli_main([])
    finally:
        os.chdir(original_cwd)

    assert exit_code == 0
    with sqlite3.connect(output_path) as connection:
        row = connection.execute(
            "SELECT activitypub_username, private_key_pem FROM users"
        ).fetchone()

    assert row == ("dave", "PRIVATE-KEY-DAVE")
