"""Behavior scenarios for local-community registration and command policy."""

from __future__ import annotations

import sqlite3
import pytest


from pathlib import Path

from sqlalchemy import create_engine, text

from src.config import Settings
from src.db import Database
from src.local_communities.service import LocalCommunityService
from src.operations.create_community import (
    CreateCommunityInput,
    create_community_operation,
)
from support.db import build_database


def _settings(*, allowlist: str = "123") -> Settings:
    """Build one Settings instance with the local-community operator allowlist."""
    return Settings.model_construct(
        discord_token="discord-token",
        database_url="sqlite:///./bridge.db",
        fedify_gateway_url="http://127.0.0.1:3000",
        internal_http_host="127.0.0.1",
        internal_http_port=8080,
        public_base_url="https://bridge.example",
        discord_oauth_client_id="",
        discord_oauth_client_secret="",
        discord_oauth_redirect_uri=None,
        registration_session_cookie_name="bridge_registration_session",
        registration_session_ttl_seconds=3600,
        fedify_shared_secret="secret",
        bridge_display_prefix="[bridge]",
        log_level="INFO",
        federation_allowlist=[],
        bridge_super_admin_user_ids=allowlist.split(",") if allowlist else [],
    )


def test_allowlisted_operator_creates_local_community_and_persists_actor_metadata(
    tmp_path: Path,
) -> None:
    """An allowlisted operator should create one persisted local community row."""
    database = build_database(tmp_path, "local-community-registration.db")
    result = create_community_operation(
        CreateCommunityInput(
            database=database,
            settings=_settings(),
            discord_user_id="123",
            discord_guild_id=10,
            discord_forum_channel_id=100,
            slug="hackers",
            name="Hackers",
            description="A local hackerspace forum.",
        )
    )
    created = database.local_communities.get_local_community_by_slug("hackers")

    assert result.applied is True
    assert created is not None
    assert created.display_name == "Hackers"
    assert created.summary == "A local hackerspace forum."
    assert created.created_by_discord_user_id == "123"
    assert created.actor_url == "https://bridge.example/actors/communities/hackers"


def test_registered_policy_no_longer_uses_operator_allowlist_for_create(
    tmp_path: Path,
) -> None:
    """Creation no longer depends on the historical operator allowlist."""
    database = build_database(tmp_path, "local-community-registration-denied.db")
    result = create_community_operation(
        CreateCommunityInput(
            database=database,
            settings=_settings(allowlist="999"),
            discord_user_id="123",
            discord_guild_id=10,
            discord_forum_channel_id=100,
            slug="hackers",
            name="Hackers",
            description="A local hackerspace forum.",
        )
    )

    assert result.applied is True
    assert result.reason == "created"
    created = database.local_communities.get_local_community_by_slug("hackers")
    assert created is not None
    assert created.created_by_discord_user_id == "123"


def test_service_rejects_duplicate_forum_binding(
    tmp_path: Path,
) -> None:
    """A forum channel already bound to one local community cannot be reused."""
    database = build_database(tmp_path, "local-community-registration-duplicate.db")
    service = LocalCommunityService(
        database=database, base_url="https://bridge.example"
    )
    service.create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="hackers",
        name="Hackers",
        description="A local hackerspace forum.",
        created_by_discord_user_id="123",
    )

    try:
        service.create_local_community(
            discord_guild_id=10,
            discord_forum_channel_id=100,
            slug="makers",
            name="Makers",
            description="Another forum.",
            created_by_discord_user_id="123",
        )
    except Exception as exc:
        assert "already bound" in str(exc)
    else:
        raise AssertionError("Expected duplicate forum binding to fail")


def test_duplicate_slug_does_not_modify_existing_owner_or_create_new_row(
    tmp_path: Path,
) -> None:
    """A duplicate slug failure must not overwrite the existing community owner."""
    database = build_database(
        tmp_path, "local-community-registration-duplicate-slug.db"
    )
    first = create_community_operation(
        CreateCommunityInput(
            database=database,
            settings=_settings(),
            discord_user_id="123",
            discord_guild_id=10,
            discord_forum_channel_id=100,
            slug="hackers",
            name="Hackers",
            description="A local hackerspace forum.",
        )
    )
    second = create_community_operation(
        CreateCommunityInput(
            database=database,
            settings=_settings(allowlist="123,456"),
            discord_user_id="456",
            discord_guild_id=10,
            discord_forum_channel_id=200,
            slug="hackers",
            name="Other Hackers",
            description="Another forum.",
        )
    )
    created = database.local_communities.get_local_community_by_slug("hackers")

    assert first.applied is True
    assert second.applied is False
    assert second.reason == "validation_failed"
    assert created.created_by_discord_user_id == "123"
    assert (
        database.local_communities.get_local_community_by_forum_channel_id(200) is None
    )


def test_invalid_slug_does_not_create_partial_owned_row(tmp_path: Path) -> None:
    """Creation validation failures must not persist a local-community row."""
    database = build_database(tmp_path, "local-community-registration-invalid-slug.db")

    result = create_community_operation(
        CreateCommunityInput(
            database=database,
            settings=_settings(),
            discord_user_id="123",
            discord_guild_id=10,
            discord_forum_channel_id=100,
            slug="Invalid Slug",
            name="Hackers",
            description="A local hackerspace forum.",
        )
    )

    assert result.applied is False
    assert database.local_communities.list_local_communities() == []


def test_blank_name_does_not_create_partial_owned_row(tmp_path: Path) -> None:
    """Missing required display text must fail before actor state is persisted."""
    database = build_database(tmp_path, "local-community-registration-blank-text.db")

    blank_name = create_community_operation(
        CreateCommunityInput(
            database=database,
            settings=_settings(),
            discord_user_id="123",
            discord_guild_id=10,
            discord_forum_channel_id=100,
            slug="hackers",
            name=" ",
            description="A local hackerspace forum.",
        )
    )

    assert blank_name.applied is False
    assert database.local_communities.list_local_communities() == []


def test_create_local_community_allows_missing_or_blank_description(
    tmp_path: Path,
) -> None:
    """Create-community now treats omitted and blank descriptions as NULL."""
    database = build_database(tmp_path, "local-community-registration-null-summary.db")

    omitted = create_community_operation(
        CreateCommunityInput(
            database=database,
            settings=_settings(),
            discord_user_id="123",
            discord_guild_id=10,
            discord_forum_channel_id=100,
            slug="cats",
            name="Cats",
            description=None,
        )
    )
    blank = create_community_operation(
        CreateCommunityInput(
            database=database,
            settings=_settings(),
            discord_user_id="123",
            discord_guild_id=10,
            discord_forum_channel_id=101,
            slug="dogs",
            name="Dogs",
            description="   ",
        )
    )

    assert omitted.applied is True
    assert blank.applied is True
    assert (
        database.local_communities.get_local_community_by_slug("cats").summary is None
    )
    assert (
        database.local_communities.get_local_community_by_slug("dogs").summary is None
    )


def test_create_local_community_rejects_too_long_description(tmp_path: Path) -> None:
    """Description length validation protects the nullable summary column."""
    database = build_database(tmp_path, "local-community-registration-long-summary.db")

    result = create_community_operation(
        CreateCommunityInput(
            database=database,
            settings=_settings(),
            discord_user_id="123",
            discord_guild_id=10,
            discord_forum_channel_id=100,
            slug="cats",
            name="Cats",
            description="x" * 1001,
        )
    )

    assert result.applied is False
    assert result.message == "Community summary must be 1000 characters or fewer."
    assert database.local_communities.list_local_communities() == []


def test_existing_database_unversioned_startup_preserves_nullable_owner_column(
    tmp_path: Path,
) -> None:
    """Migration adds creator ownership to existing local-community tables."""
    db_path = tmp_path / "legacy-local-communities.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.execute(text("""
                CREATE TABLE channel_community_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_channel_id INTEGER NOT NULL,
                    lemmy_community_actor_id VARCHAR(512) NOT NULL,
                    lemmy_community_name VARCHAR(255),
                    lemmy_community_id INTEGER,
                    community_handle VARCHAR(255),
                    community_inbox_url VARCHAR(512),
                    follow_activity_id VARCHAR(512),
                    initiated_by_discord_user_id VARCHAR(64),
                    status VARCHAR(32) NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """))
        conn.execute(text("""
                CREATE TABLE local_communities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_guild_id INTEGER NOT NULL,
                    discord_forum_channel_id INTEGER NOT NULL,
                    slug VARCHAR(255) NOT NULL,
                    display_name VARCHAR(255) NOT NULL,
                    summary VARCHAR NOT NULL,
                    actor_url VARCHAR(512) NOT NULL,
                    inbox_url VARCHAR(512) NOT NULL,
                    outbox_url VARCHAR(512) NOT NULL,
                    followers_url VARCHAR(512) NOT NULL,
                    public_key_pem VARCHAR NOT NULL,
                    private_key_pem VARCHAR NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """))

        conn.execute(
            text(
                "CREATE TABLE local_community_threads (id INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE local_community_messages (id INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
        )
        conn.execute(text("""
                INSERT INTO local_communities (
                    discord_guild_id,
                    discord_forum_channel_id,
                    slug,
                    display_name,
                    summary,
                    actor_url,
                    inbox_url,
                    outbox_url,
                    followers_url,
                    public_key_pem,
                    private_key_pem,
                    status,
                    created_at,
                    updated_at
                ) VALUES (
                    10,
                    100,
                    'cats',
                    'Cats',
                    'Cat photos.',
                    'https://bridge.example/actors/communities/cats',
                    'https://bridge.example/actors/communities/cats/inbox',
                    'https://bridge.example/actors/communities/cats/outbox',
                    'https://bridge.example/actors/communities/cats/followers',
                    'public-key',
                    'private-key',
                    'active',
                    '2026-01-01 00:00:00',
                    '2026-01-01 00:00:00'
                )
                """))
    engine.dispose()

    database = Database(f"sqlite:///{db_path}")
    with database.engine.connect() as connection:
        schema_before = connection.exec_driver_sql(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
        ).all()
        names = [row[1] for row in schema_before if row[0] == "table"]
        rows_before = {
            name: connection.exec_driver_sql(
                f'SELECT * FROM "{name}" ORDER BY rowid'
            ).all()
            for name in names
        }
    with pytest.raises(RuntimeError, match="baseline"):
        database.migrate()
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == 0
        assert (
            connection.exec_driver_sql(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
            ).all()
            == schema_before
        )
        for name in names:
            assert (
                connection.exec_driver_sql(
                    f'SELECT * FROM "{name}" ORDER BY rowid'
                ).all()
                == rows_before[name]
            )
    database.close()


def test_owner_column_migration_is_idempotent(tmp_path: Path) -> None:
    """Running migration repeatedly must preserve data and avoid duplicate columns."""
    db_path = tmp_path / "idempotent-local-communities.db"
    database = Database(f"sqlite:///{db_path}")
    database.migrate()
    database.migrate()

    with database.engine.connect() as conn:
        columns = conn.execute(text("PRAGMA table_info(local_communities)")).fetchall()
    owner_columns = [row for row in columns if row[1] == "created_by_discord_user_id"]

    assert len(owner_columns) == 1


def test_summary_nullable_unversioned_startup_preserves_constraints(
    tmp_path: Path,
) -> None:
    """Rebuilt local_communities table must keep identity uniqueness constraints."""
    db_path = tmp_path / "summary-nullable-constraints.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE local_community_threads (id INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE local_community_messages (id INTEGER PRIMARY KEY AUTOINCREMENT)"
            )
        )
        conn.execute(text("""
                CREATE TABLE channel_community_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_channel_id INTEGER NOT NULL,
                    lemmy_community_actor_id VARCHAR(512) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """))
        conn.execute(text("""
                CREATE TABLE local_communities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_guild_id INTEGER NOT NULL,
                    discord_forum_channel_id INTEGER NOT NULL UNIQUE,
                    slug VARCHAR(255) NOT NULL UNIQUE,
                    display_name VARCHAR(255) NOT NULL,
                    summary VARCHAR NOT NULL,
                    actor_url VARCHAR(512) NOT NULL UNIQUE,
                    inbox_url VARCHAR(512) NOT NULL,
                    outbox_url VARCHAR(512) NOT NULL,
                    followers_url VARCHAR(512) NOT NULL,
                    public_key_pem VARCHAR NOT NULL,
                    private_key_pem VARCHAR NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """))
        conn.execute(text("""
                INSERT INTO local_communities (
                    discord_guild_id, discord_forum_channel_id, slug, display_name, summary,
                    actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem,
                    status, created_at, updated_at
                ) VALUES (
                    10, 100, 'cats', 'Cats', 'Cat photos.',
                    'https://bridge.example/actors/communities/cats',
                    'https://bridge.example/actors/communities/cats/inbox',
                    'https://bridge.example/actors/communities/cats/outbox',
                    'https://bridge.example/actors/communities/cats/followers',
                    'public-key', 'private-key', 'active', '2026-01-01 00:00:00', '2026-01-01 00:00:00'
                )
                """))
    engine.dispose()

    database = Database(f"sqlite:///{db_path}")
    with database.engine.connect() as connection:
        schema_before = connection.exec_driver_sql(
            "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
        ).all()
        names = [row[1] for row in schema_before if row[0] == "table"]
        rows_before = {
            name: connection.exec_driver_sql(
                f'SELECT * FROM "{name}" ORDER BY rowid'
            ).all()
            for name in names
        }
    with pytest.raises(RuntimeError, match="baseline"):
        database.migrate()
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA user_version").scalar_one() == 0
        assert (
            connection.exec_driver_sql(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' ORDER BY type,name"
            ).all()
            == schema_before
        )
        for name in names:
            assert (
                connection.exec_driver_sql(
                    f'SELECT * FROM "{name}" ORDER BY rowid'
                ).all()
                == rows_before[name]
            )
    database.close()


def test_fresh_database_local_community_summary_is_nullable(tmp_path: Path) -> None:
    """Fresh schema created from current models should allow NULL summaries."""
    database = build_database(tmp_path, "fresh-null-summary.db")

    with database.engine.connect() as conn:
        summary_column = [
            row
            for row in conn.execute(
                text("PRAGMA table_info(local_communities)")
            ).fetchall()
            if row[1] == "summary"
        ][0]

    assert summary_column[3] == 0
