"""Export registered local users into a standalone SQLite backup database.

This module owns both the backup logic and the small CLI contract used by
operators. The command intentionally defaults to the project-local SQLite DB
and a predictable backup path so a one-command identity backup stays simple.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, User

DEFAULT_DATABASE_URL = "sqlite:///./bridge.db"
DEFAULT_OUTPUT_PATH = Path("dev/users.db.backup")


class UserIdentityBackupError(RuntimeError):
    """Signal that the requested user-only backup cannot be produced safely."""


def write_user_identity_backup(
    *,
    source_database_url: str,
    output_path: Path,
) -> Path:
    """Write a standalone SQLite backup DB that contains only the `users` table.

    The backup preserves the exact user identity rows, including private keys,
    so the file can later be attached and merged back into a fresh bridge DB.
    """

    source_path = resolve_sqlite_path(source_database_url)
    if not source_path.exists():
        raise UserIdentityBackupError(
            f"Source database does not exist: {source_path}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    source_engine = create_engine(
        f"sqlite:///{source_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(
        source_engine,
        expire_on_commit=False,
        class_=Session,
    )

    # The backup DB intentionally contains only the users table so operators can
    # restore identity continuity without dragging unrelated runtime state along.
    backup_connection = sqlite3.connect(output_path)
    try:
        create_users_table(backup_connection)
        with session_factory() as session:
            users = list(session.scalars(select(User).order_by(User.created_at, User.id)))
        insert_users(backup_connection, users)
        backup_connection.commit()
    finally:
        backup_connection.close()
        source_engine.dispose()

    return output_path


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the user-only identity backup command.

    The defaults match the common local developer workflow: run the command
    from the project root and get one `dev/users.db.backup` artifact.
    """

    parser = argparse.ArgumentParser(
        description=(
            "Export registered local users and keypairs into a standalone "
            "SQLite backup DB that can later be attached and restored."
        )
    )
    parser.add_argument(
        "--database-url",
        default=DEFAULT_DATABASE_URL,
        help="SQLAlchemy database URL to read from.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Path to the SQLite backup file to write.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Export the current local registered-user identities to one SQLite file.

    The CLI prints the written path so shell scripts can chain this command
    without parsing extra status text.
    """

    args = build_parser().parse_args(argv)
    output_path = Path(args.output)

    # The backup intentionally includes private keys because actor continuity
    # depends on restoring the exact keypair, not only the public metadata.
    written_path = write_user_identity_backup(
        source_database_url=args.database_url,
        output_path=output_path,
    )
    print(written_path)
    return 0


def resolve_sqlite_path(database_url: str) -> Path:
    """Resolve a local filesystem path from a sqlite:/// SQLAlchemy database URL."""

    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite":
        raise UserIdentityBackupError(
            f"Only sqlite:/// database URLs are supported, got: {database_url}"
        )
    if parsed.netloc not in {"", None}:
        raise UserIdentityBackupError(
            f"Unsupported sqlite database URL format: {database_url}"
        )

    # SQLAlchemy's common relative form `sqlite:///./bridge.db` reaches urlparse
    # as `/./bridge.db`. Treat that as cwd-relative instead of filesystem-rooted,
    # otherwise the default CLI would incorrectly look for `/bridge.db`.
    raw_path = parsed.path
    if raw_path.startswith("/./"):
        return Path(raw_path[1:]).resolve()
    if raw_path == "/.":
        return Path(".").resolve()
    return Path(raw_path).resolve()


def create_users_table(connection: sqlite3.Connection) -> None:
    """Create the exact backup table shape needed for later user restoration."""

    # The backup schema mirrors only the user identity table, because the goal
    # is to preserve actor continuity while keeping the artifact narrow.
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER NOT NULL PRIMARY KEY,
            discord_user_id VARCHAR(64) NOT NULL UNIQUE,
            activitypub_username VARCHAR(255) NOT NULL UNIQUE,
            actor_url VARCHAR(512) NOT NULL UNIQUE,
            inbox_url VARCHAR(512) NOT NULL,
            outbox_url VARCHAR(512) NOT NULL,
            followers_url VARCHAR(512) NOT NULL,
            public_key_pem TEXT NOT NULL,
            private_key_pem TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        """
    )


def insert_users(connection: sqlite3.Connection, users: list[User]) -> None:
    """Insert the exported user identity rows into the backup database."""

    connection.executemany(
        """
        INSERT INTO users (
            id,
            discord_user_id,
            activitypub_username,
            actor_url,
            inbox_url,
            outbox_url,
            followers_url,
            public_key_pem,
            private_key_pem,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                user.id,
                user.discord_user_id,
                user.activitypub_username,
                user.actor_url,
                user.inbox_url,
                user.outbox_url,
                user.followers_url,
                user.public_key_pem,
                user.private_key_pem,
                user.created_at.isoformat(),
                user.updated_at.isoformat(),
            )
            for user in users
        ],
    )
