"""Atomic released-v0 upgrades and current-v1 validation for bridge SQLite.

The runner owns the writer lock, header and FK suspension. Historical
source staging, ORM target creation and SQL transfer share its single physical
transaction. The direct migration module owns transfer and protocol evidence.
"""

from __future__ import annotations

from collections.abc import Callable
import re

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from .legacy_identity_cutover import (
    IdentityCutoverConfig,
    validate_current_identity,
)

from .legacy_v0_to_v1 import upgrade_released_v0_to_v1, validate_released_v0


def _normalized_sql(sql):
    """Normalize incidental whitespace and identifier quoting, preserving literals."""
    tokens = re.findall(
        r"'(?:''|[^'])*'|\"[^\"]*\"|`[^`]*`|\[[^\]]*\]|\w+|[^\s]", sql or ""
    )
    return " ".join(t if t.startswith("'") else t.strip('"`[]').lower() for t in tokens)


def _table_manifest(conn: Connection, name: str) -> dict:
    """Compare complete SQLite DDL and native physical schema evidence.

    Keep literals intact. Only identifier quoting, whitespace and constraint
    labels are incidental; generated expressions and STORED remain in full DDL.
    """
    ddl = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).scalar_one()
    ddl = re.sub(
        r"('(?:''|[^'])*')|\bconstraint [a-z_][a-z_0-9]* (?=(?:check|unique|primary|foreign)\b)",
        lambda match: match.group(1) or "",
        _normalized_sql(ddl),
    )
    indexes = []
    for index in conn.exec_driver_sql(f'PRAGMA index_list("{name}")'):
        sql = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name=?", (index[1],)
        ).scalar_one()
        indexes.append(
            (
                index[1],
                index[2],
                index[3],
                index[4],
                tuple(conn.exec_driver_sql(f'PRAGMA index_xinfo("{index[1]}")').all()),
                _normalized_sql(sql),
            )
        )
    return {
        "ddl": ddl,
        "columns": tuple(conn.exec_driver_sql(f'PRAGMA table_xinfo("{name}")').all()),
        "fks": tuple(conn.exec_driver_sql(f'PRAGMA foreign_key_list("{name}")').all()),
        "indexes": tuple(sorted(indexes)),
        "triggers": tuple(
            (row[0], _normalized_sql(row[1]))
            for row in conn.exec_driver_sql(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? ORDER BY name",
                (name,),
            )
        ),
    }


def _require_sqlite_model_support(conn: Connection) -> None:
    """Reject unsupported SQLite before any schema or data modification."""
    version = tuple(
        int(part)
        for part in conn.exec_driver_sql("SELECT sqlite_version()")
        .scalar_one()
        .split(".")
    )
    if version < (3, 31, 0):
        raise RuntimeError("Stage 01 requires SQLite >= 3.31.0 for STORED role")


def _validate_current_v1_schema(conn: Connection) -> None:
    """Validate all target columns, constraints, indexes and triggers without repairing them."""
    from ..models import Base

    if _user_tables(conn) != set(Base.metadata.tables):
        raise RuntimeError(
            "Not the current v1 schema (obsolete development/partial database); restore a supported release backup"
        )
    if conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='view'").first():
        raise RuntimeError("Unexpected current v1 schema view")
    from sqlalchemy import create_engine
    from . import schema

    # Let SQLite construct the reference from the same ORM and deployment
    # objects. No hand-built duplicate of model constraints is maintained.
    reference_engine = create_engine("sqlite://")
    try:
        with reference_engine.connect() as reference:
            schema.create_all(reference)
            _install_current_schema_objects(reference)
            for name in sorted(Base.metadata.tables):
                actual = _table_manifest(conn, name)
                for boundary, expected in _table_manifest(reference, name).items():
                    if actual[boundary] != expected:
                        # Retain diagnostic boundaries used by startup callers;
                        # complete column/constraint DDL includes generated role.
                        label = "columns" if boundary == "ddl" else boundary
                        raise RuntimeError(
                            f"Not the current v1 schema: {name}.{label}; "
                            "restore a verified backup"
                        )
    finally:
        reference_engine.dispose()
    _validate_sqlite_integrity(conn, "Current v1")
    if (
        conn.exec_driver_sql(
            "SELECT count(*) FROM platform_connectors WHERE connector_key='discord-primary' AND platform='discord' AND instance_origin IS NULL"
        ).scalar_one()
        != 1
    ):
        raise RuntimeError("Current v1 requires exactly one primary Discord connector")


def _install_current_schema_objects(conn: Connection) -> None:
    """Install retained permanent profile and authored-object indexes on one connection."""
    _install_profile_media_triggers(conn)
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_published_activity_objects_conversation_root_object_id ON published_activity_objects (conversation_root_object_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_published_objects_user_outbox ON published_activity_objects (actor_username,actor_url,deleted_at,published_at,id)"
    )


def _validate_sqlite_integrity(conn: Connection, label: str) -> None:
    """Check structural and row-level integrity without logging sensitive row contents."""
    violations = conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    if violations:
        raise RuntimeError(f"{label} foreign-key violations: {violations}")
    if conn.exec_driver_sql("PRAGMA integrity_check").scalars().all() != ["ok"]:
        raise RuntimeError(f"{label} SQLite integrity check failed")


DATABASE_VERSION = 1
MIGRATIONS: dict[int, Callable[[Connection, IdentityCutoverConfig | None], None]] = {
    1: upgrade_released_v0_to_v1
}


def _validate_registry() -> None:
    """Require exactly the supported consecutive integer callable upgrade set."""
    if type(DATABASE_VERSION) is not int or not 1 <= DATABASE_VERSION <= 2147483647:
        raise RuntimeError("Invalid database migration registry target version")
    if (
        any(type(v) is not int for v in MIGRATIONS)
        or len(MIGRATIONS) != DATABASE_VERSION
        or set(MIGRATIONS) != set(range(1, DATABASE_VERSION + 1))
        or any(not callable(step) for step in MIGRATIONS.values())
    ):
        raise RuntimeError(
            "Invalid database migration registry: expected consecutive callable upgrades"
        )


def _read_version(conn: Connection) -> int:
    """Reject unsupported headers before allowing any schema writes."""
    version = int(conn.exec_driver_sql("PRAGMA user_version").scalar_one())
    if version < 0 or version > DATABASE_VERSION:
        raise RuntimeError(
            f"Unsupported database version {version}; application supports {DATABASE_VERSION}"
        )
    return version


def _user_tables(conn: Connection) -> set[str]:
    """Distinguish an empty file from every nonempty application shape."""
    return set(
        conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'"
        ).scalars()
    )


def read_database_state(
    engine: Engine, identity: IdentityCutoverConfig | None = None
) -> tuple[int, bool]:
    """Perform read-only source/target preflight; locked inspection remains authoritative."""
    _validate_registry()
    if engine.dialect.name != "sqlite":
        raise RuntimeError("Database migrations support SQLite only")
    with engine.connect() as conn:
        _require_sqlite_model_support(conn)
        if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
            raise RuntimeError("Database preflight requires foreign_keys=ON")
        # SQLite legacy transaction mode does not give SELECT a stable snapshot.
        # Pin the advisory inspection so a concurrent commit cannot mix schemas.
        conn.rollback()
        conn.exec_driver_sql("BEGIN")
        version = _read_version(conn)
        empty = not _user_tables(conn)
        if version == 1:
            _validate_current_v1_schema(conn)
            validate_current_identity(conn, identity)
        elif not empty:
            validate_released_v0(conn)
        elif conn.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='view'"
        ).first():
            raise RuntimeError("Unsupported empty database with schema objects")
        return version, empty


def _rollback_physical_transaction(conn: Connection) -> None:
    """Roll back SQLite even if a failed SQLAlchemy commit lost its logical transaction.

    A commit event/driver error can deactivate the wrapper before SQLite ends its
    explicit BEGIN. The physical state must be closed before PRAGMA restoration.
    """
    conn.rollback()
    driver = conn.connection.driver_connection
    if driver.in_transaction:
        driver.rollback()


def _restore_foreign_keys(conn: Connection) -> None:
    """Never return a connection to its pool with suspended or unverified enforcement."""
    try:
        _rollback_physical_transaction(conn)
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
            raise RuntimeError("FK pragma remained disabled")
    except BaseException as exc:
        conn.invalidate()
        raise RuntimeError("Failed to restore SQLite FK enforcement") from exc


def run_pending_migrations(
    engine: Engine, identity: IdentityCutoverConfig | None = None
) -> None:
    """Initialize empty files or upgrade released v0 with one runner-owned transaction.

    Advisory classification never authorizes writes. Reread header and physical
    inventory under BEGIN IMMEDIATE; a stale contender validates the winner.
    Fresh bootstrap keeps FK ON, and released-source rebuilds suspend it only
    before the transaction. The sole header write follows every validation.
    """
    from . import schema

    version, empty = read_database_state(engine, identity)
    if version == DATABASE_VERSION:
        return
    with engine.connect() as conn:
        suspended = False
        try:
            conn.rollback()
            if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                raise RuntimeError("Upgrade connection lacks FK enforcement")
            conn.rollback()
            if not empty:
                suspended = True
                conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
                if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
                    raise RuntimeError(
                        "Could not suspend FK enforcement before upgrade"
                    )
                conn.rollback()
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            version = _read_version(conn)
            tables = _user_tables(conn)
            if version == DATABASE_VERSION:
                # Another starter committed while this one waited for the writer.
                _validate_current_v1_schema(conn)
            elif not tables:
                if suspended:
                    raise RuntimeError(
                        "Source disappeared during startup; retry against verified file"
                    )
                schema.create_all(conn)
                _install_current_schema_objects(conn)
                conn.exec_driver_sql(
                    "INSERT INTO platform_connectors(connector_key,platform,instance_origin) VALUES('discord-primary','discord',NULL)"
                )
                _validate_current_v1_schema(conn)
                conn.exec_driver_sql("PRAGMA user_version = 1")
            else:
                if not suspended:
                    raise RuntimeError(
                        "Source changed during bootstrap; retry against verified file"
                    )
                MIGRATIONS[1](conn, identity)
                _validate_current_v1_schema(conn)
                conn.exec_driver_sql("PRAGMA user_version = 1")
            conn.commit()
        except BaseException:
            _rollback_physical_transaction(conn)
            raise
        finally:
            if suspended:
                _restore_foreign_keys(conn)
    # A migrated/current connection must be rechecked after FK restoration.
    read_database_state(engine, identity)


def _table_columns(conn: Connection, table: str) -> set[str]:
    """Return the current SQLite column names for one table."""
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {str(row[1]) for row in rows}


def _install_profile_media_triggers(conn: Connection) -> None:
    """Enforce slot-to-kind consistency for every SQLite write path."""
    if not _table_columns(conn, "users") or not _table_columns(
        conn, "profile_media_assets"
    ):
        return
    conn.execute(text("""
        CREATE TRIGGER IF NOT EXISTS users_avatar_media_kind_insert
        BEFORE INSERT ON users WHEN NEW.avatar_media_id IS NOT NULL
        BEGIN
          SELECT CASE WHEN (SELECT media_kind FROM profile_media_assets WHERE id = NEW.avatar_media_id) != 'avatar'
            THEN RAISE(ABORT, 'avatar_media_id must reference avatar media') END;
        END
    """))
    conn.execute(text("""
        CREATE TRIGGER IF NOT EXISTS users_avatar_media_kind_update
        BEFORE UPDATE OF avatar_media_id ON users WHEN NEW.avatar_media_id IS NOT NULL
        BEGIN
          SELECT CASE WHEN (SELECT media_kind FROM profile_media_assets WHERE id = NEW.avatar_media_id) != 'avatar'
            THEN RAISE(ABORT, 'avatar_media_id must reference avatar media') END;
        END
    """))
    conn.execute(text("""
        CREATE TRIGGER IF NOT EXISTS users_header_media_kind_insert
        BEFORE INSERT ON users WHEN NEW.header_media_id IS NOT NULL
        BEGIN
          SELECT CASE WHEN (SELECT media_kind FROM profile_media_assets WHERE id = NEW.header_media_id) != 'header'
            THEN RAISE(ABORT, 'header_media_id must reference header media') END;
        END
    """))
    conn.execute(text("""
        CREATE TRIGGER IF NOT EXISTS users_header_media_kind_update
        BEFORE UPDATE OF header_media_id ON users WHEN NEW.header_media_id IS NOT NULL
        BEGIN
          SELECT CASE WHEN (SELECT media_kind FROM profile_media_assets WHERE id = NEW.header_media_id) != 'header'
            THEN RAISE(ABORT, 'header_media_id must reference header media') END;
        END
    """))
