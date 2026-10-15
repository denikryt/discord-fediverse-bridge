"""Versioned upgrades and retained schema helpers for bridge SQLite databases.

Database owns startup orchestration. Registered upgrades reserve the SQLite
writer and commit each schema/data step with its header version. Retained
baseline schema helpers execute separately with their existing limitations;
historical ActivityPub identity conversion is not part of startup.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..models import MessageMapping, PublishedActivityObject, UserActorOutboundSource


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


def _validate_v2_schema(conn: Connection) -> None:
    """Audit actual FK data and the stored generated claim column before commit."""
    from ..models import Base

    actual_tables = set(
        conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).scalars()
    )
    if not set(Base.metadata.tables).issubset(actual_tables):
        raise RuntimeError(
            "Incomplete current database schema; restore a verified backup"
        )
    objects = set(
        conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type IN ('index','trigger')"
        ).scalars()
    )
    mandatory = {
        index.name for table in Base.metadata.tables.values() for index in table.indexes
    } | {
        "ix_published_activity_objects_conversation_root_object_id",
        "ix_published_objects_user_outbox",
        "users_avatar_media_kind_insert",
        "users_avatar_media_kind_update",
        "users_header_media_kind_insert",
        "users_header_media_kind_update",
    }
    if not mandatory.issubset(objects):
        raise RuntimeError(
            "Incomplete current database schema objects; restore a verified backup"
        )
    # Compare declared FK groups with the actual SQLite definition. A clean
    # data audit alone cannot prove a missing composite constraint exists.
    for table in Base.metadata.tables.values():
        if not ({"connector_id", "source_connector_id"} & set(table.columns.keys())):
            continue
        groups = {}
        for row in conn.exec_driver_sql(f'PRAGMA foreign_key_list("{table.name}")'):
            groups.setdefault(row[0], []).append(row)
        actual = {
            (
                rows[0][2],
                tuple(r[3] for r in sorted(rows, key=lambda r: r[1])),
                tuple(r[4] for r in sorted(rows, key=lambda r: r[1])),
                rows[0][6],
                rows[0][5],
            )
            for rows in groups.values()
        }
        expected = {
            (
                fk.elements[0].column.table.name,
                tuple(e.parent.name for e in fk.elements),
                tuple(e.column.name for e in fk.elements),
                fk.ondelete or "NO ACTION",
                fk.onupdate or "NO ACTION",
            )
            for fk in table.foreign_key_constraints
        }
        if not expected.issubset(actual):
            raise RuntimeError(f"Missing v2 foreign-key definition: {table.name}")
    ddl = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE name='platform_channel_surfaces'"
    ).scalar_one()
    expression = str(
        Base.metadata.tables["platform_channel_surfaces"].c.role.computed.sqltext
    )
    if "".join(expression.split()) not in "".join(ddl.split()):
        raise RuntimeError("V2 generated role expression differs from the model")
    columns = conn.exec_driver_sql(
        "PRAGMA table_xinfo(platform_channel_surfaces)"
    ).all()
    if not any(row[1] == "role" and row[6] == 3 for row in columns):
        raise RuntimeError("V2 schema lacks STORED generated ChannelSurface.role")
    if conn.exec_driver_sql("PRAGMA foreign_key_check").all():
        raise RuntimeError("V2 migration contains foreign-key violations")
    if conn.exec_driver_sql("PRAGMA integrity_check").scalars().all() != ["ok"]:
        raise RuntimeError("V2 SQLite integrity check failed")
    if (
        conn.exec_driver_sql(
            "SELECT count(*) FROM platform_connectors WHERE connector_key='discord-primary' AND platform='discord' AND instance_origin IS NULL"
        ).scalar_one()
        != 1
    ):
        raise RuntimeError("V2 schema requires exactly one primary Discord connector")


def _install_current_schema_objects(conn: Connection) -> None:
    """Install retained permanent profile and authored-object indexes on one connection."""
    _install_profile_media_triggers(conn)
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_published_activity_objects_conversation_root_object_id ON published_activity_objects (conversation_root_object_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_published_objects_user_outbox ON published_activity_objects (actor_username,actor_url,deleted_at,published_at,id)"
    )


def bootstrap_current_schema_v2(engine: Engine) -> None:
    """Create schema, connector and version atomically, rechecking after reservation."""
    from . import schema

    _validate_registry()
    upgrade_existing = False
    with engine.connect() as conn:
        _require_sqlite_model_support(conn)
        conn.rollback()
        try:
            if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                raise RuntimeError("Fresh bootstrap requires foreign_keys=ON")
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            version = _read_version(conn)
            if version == 0:
                if conn.exec_driver_sql(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*' LIMIT 1"
                ).first():
                    raise RuntimeError(
                        "Unversioned database requires confirmed baseline"
                    )
                schema.create_all(conn)
                _install_current_schema_objects(conn)
                conn.exec_driver_sql(
                    "INSERT INTO platform_connectors(connector_key,platform,instance_origin) VALUES('discord-primary','discord',NULL)"
                )
                _validate_v2_schema(conn)
                if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                    raise RuntimeError("Fresh v2 bootstrap requires foreign_keys=ON")
                conn.exec_driver_sql(f"PRAGMA user_version={DATABASE_VERSION}")
            elif version == DATABASE_VERSION:
                _validate_v2_schema(conn)
            else:
                # Another initializer adopted v1 while we waited. End this
                # transaction before entering the registered upgrade runner.
                upgrade_existing = True
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    if upgrade_existing:
        run_pending_migrations(engine)


def _rebuild_v2_table(conn: Connection, name: str, rows: list[dict]) -> None:
    """Replace one approved table while retaining original IDs and explicit schema objects.

    Called only by the FK-suspended v2 upgrade. Copy DML excludes computed
    columns and preserves stored values without datetime/JSON reinterpretation.
    """
    from sqlalchemy.schema import CreateTable, CreateIndex
    from ..models import Base

    table = Base.metadata.tables[name]
    quote = conn.dialect.identifier_preparer.quote_identifier
    replacement = "_new_" + name
    ddl = str(CreateTable(table).compile(dialect=conn.dialect))
    ddl = ddl.replace("CREATE TABLE " + name, "CREATE TABLE " + quote(replacement), 1)
    saved = conn.exec_driver_sql(
        "SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",
        (name,),
    ).all()
    conn.exec_driver_sql(ddl)
    if rows:
        columns = [column.name for column in table.columns if column.computed is None]
        sql = f"INSERT INTO {quote(replacement)} ({','.join(quote(c) for c in columns)}) VALUES ({','.join('?' for _ in columns)})"
        conn.exec_driver_sql(sql, [tuple(row.get(c) for c in columns) for row in rows])
    conn.exec_driver_sql(f"DROP TABLE {quote(name)}")
    conn.exec_driver_sql(f"ALTER TABLE {quote(replacement)} RENAME TO {quote(name)}")
    for index in table.indexes:
        conn.exec_driver_sql(str(CreateIndex(index).compile(dialect=conn.dialect)))
    # Explicit deployment indexes/triggers are not silently discarded. Indexes
    # defined in current metadata were already installed on the replacement.
    current_indexes = {index.name for index in table.indexes}
    for kind, object_name, sql in saved:
        if kind != "index" or object_name not in current_indexes:
            conn.exec_driver_sql(sql)


def _upgrade_platform_model_v2(conn: Connection) -> None:
    """Backfill approved v1 platform locations without changing canonical identities.

    Runner owns FK suspension and the physical transaction. Every baseline
    row is snapshotted before DDL and compared on its unchanged columns before
    the runner can stamp version 2; malformed origins or ownership fail closed.
    """
    from datetime import datetime, timezone
    from ..models import Base, PlatformConnector, ChannelSurface

    _require_sqlite_model_support(conn)
    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError("V2 parent rebuild requires pre-transaction FK suspension")
    if conn.exec_driver_sql("PRAGMA foreign_key_check").all():
        raise RuntimeError("V1 baseline contains foreign-key violations")
    names = set(
        conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'"
        ).scalars()
    )
    expected = set(Base.metadata.tables) - {
        "platform_connectors",
        "platform_channel_surfaces",
    }
    if names != expected:
        raise RuntimeError(
            f"Unsupported v1 schema tables: missing={sorted(expected-names)}, extra={sorted(names-expected)}"
        )
    snapshot = {
        name: [
            dict(row)
            for row in conn.exec_driver_sql(
                f'SELECT * FROM "{name}" ORDER BY id'
            ).mappings()
        ]
        for name in sorted(names)
    }
    # Known actor/domain tables have no v1 FK dependents. Published objects
    # have exactly the documented cascade children; do not trust ORM metadata
    # instead of inspecting the deployed schema.
    owners = {
        "local_communities",
        "local_subscribers",
        "channel_community_subscriptions",
        "personal_feed_bindings",
    }
    pao_children = {
        ("user_actor_outbound_sources", "published_object_id"),
        ("personal_feed_thread_mappings", "root_published_object_id"),
        ("inbound_discord_deliveries", "root_published_object_id"),
        ("user_actor_content_mutations", "published_object_id"),
    }
    found = set()
    for name in names:
        for fk in conn.exec_driver_sql(f'PRAGMA foreign_key_list("{name}")'):
            if fk[2] in owners:
                raise RuntimeError(
                    f"Unexpected inbound v1 FK: {name}.{fk[3]} -> {fk[2]}"
                )
            if fk[2] == "published_activity_objects":
                if fk[4] != "id" or fk[6] != "CASCADE":
                    raise RuntimeError(
                        f"Unexpected published-object FK semantics: {name}"
                    )
                found.add((name, fk[3]))
    if found != pao_children:
        raise RuntimeError("Unsupported published-object cascade dependencies")
    affected = list(owners) + [
        "local_community_thread_surfaces",
        "local_community_message_surfaces",
        "community_thread_groups",
        "community_message_groups",
        "community_thread_group_deliveries",
        "community_message_group_deliveries",
        "post_links",
        "comment_links",
        "message_mappings",
        "published_activity_objects",
        "personal_feed_thread_mappings",
        "inbound_discord_deliveries",
    ]
    # Fail on partial upgrades or missing baseline fields before creating v2.
    for name in affected:
        old_columns = _table_columns(conn, name)
        new_columns = set(Base.metadata.tables[name].columns.keys())
        additions = {
            c
            for c in new_columns
            if c.startswith(("external_", "source_external_"))
            or c
            in {
                "connector_id",
                "channel_surface_id",
                "source_connector_id",
                "source_channel_surface_id",
            }
        }
        expected_columns = new_columns - additions
        if name == "local_communities":
            expected_columns |= {"discord_guild_id", "discord_forum_channel_id"}
        if old_columns != expected_columns:
            raise RuntimeError(
                f"Unsupported or partially upgraded v1 columns in {name}"
            )
    for name, required in [
        ("local_subscribers", {"discord_channel_id"}),
        ("channel_community_subscriptions", {"discord_channel_id"}),
        ("personal_feed_bindings", {"discord_channel_id", "discord_guild_id"}),
    ]:
        info = {r[1]: r for r in _table_info(conn, name)}
        if any(info[column][3] != 1 for column in required):
            raise RuntimeError(f"Partially upgraded v1 owner table: {name}")
    threads = {row["id"]: row for row in snapshot["local_community_thread_surfaces"]}
    for row in snapshot["local_community_message_surfaces"]:
        parent = threads.get(row["local_community_thread_surface_id"])
        if (
            parent is None
            or parent["discord_forum_channel_id"] != row["discord_forum_channel_id"]
        ):
            raise RuntimeError(
                f"Message surface {row['id']} does not share its parent's channel"
            )
    PlatformConnector.__table__.create(conn)
    conn.exec_driver_sql(
        "INSERT INTO platform_connectors(connector_key,platform,instance_origin) VALUES('discord-primary','discord',NULL)"
    )
    connector = conn.exec_driver_sql("SELECT id FROM platform_connectors").scalar_one()
    ChannelSurface.__table__.create(conn)
    now = datetime.now(timezone.utc).isoformat()
    locations: dict[int, dict] = {}

    def inventory(channel, guild=None, kind="unknown"):
        """Merge only compatible persisted evidence; never invent a guild/kind."""
        if channel is None:
            return
        if (
            not isinstance(channel, int)
            or channel <= 0
            or (guild is not None and (not isinstance(guild, int) or guild <= 0))
        ):
            raise RuntimeError(f"Invalid legacy Discord location {channel}/{guild}")
        info = locations.setdefault(channel, {"guild": None, "kind": "unknown"})
        if guild is not None:
            if info["guild"] is not None and info["guild"] != guild:
                raise RuntimeError(f"Conflicting guilds for Discord channel {channel}")
            info["guild"] = guild
        if kind != "unknown":
            if info["kind"] != "unknown" and info["kind"] != kind:
                raise RuntimeError(
                    f"Conflicting channel kinds for Discord channel {channel}"
                )
            info["kind"] = kind

    channel_fields = {
        "discord_channel_id",
        "discord_forum_channel_id",
        "source_channel_id",
        "destination_channel_id",
        "root_discord_channel_id",
    }
    for name, rows in snapshot.items():
        for row in rows:
            kind = (
                "forum"
                if name
                in {
                    "local_communities",
                    "local_subscribers",
                    "channel_community_subscriptions",
                    "local_community_thread_surfaces",
                    "local_community_message_surfaces",
                    "community_thread_group_deliveries",
                    "community_message_group_deliveries",
                    "post_links",
                }
                else "unknown"
            )
            if name == "discord_channel_snapshots" and row["channel_type"] in {
                "forum",
                "text",
                "announcement",
                "news",
            }:
                kind = (
                    "announcement"
                    if row["channel_type"] == "news"
                    else row["channel_type"]
                )
            for field in channel_fields & row.keys():
                if (
                    name == "personal_feed_bindings"
                    and locations.get(row[field], {}).get("kind", "unknown") == "forum"
                ):
                    raise RuntimeError(f"Personal Feed claims a forum: {row[field]}")
                inventory(row[field], row.get("discord_guild_id"), kind)
    for channel, info in sorted(locations.items()):
        conn.exec_driver_sql(
            "INSERT INTO platform_channel_surfaces(connector_id,external_channel_id,external_guild_id,kind,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (
                connector,
                str(channel),
                str(info["guild"]) if info["guild"] is not None else None,
                info["kind"],
                now,
                now,
            ),
        )
        info["id"] = conn.exec_driver_sql("SELECT last_insert_rowid()").scalar_one()
    # Rebuild owner rows before populating typed references to them.
    for name in [
        "local_communities",
        "local_subscribers",
        "channel_community_subscriptions",
        "personal_feed_bindings",
    ]:
        _rebuild_v2_table(conn, name, snapshot[name])
    claimed = set()
    for name, column, field in [
        ("local_communities", "local_community_id", "discord_forum_channel_id"),
        ("local_subscribers", "local_subscriber_id", "discord_channel_id"),
        (
            "channel_community_subscriptions",
            "remote_subscription_id",
            "discord_channel_id",
        ),
        ("personal_feed_bindings", "personal_feed_binding_id", "discord_channel_id"),
    ]:
        for row in snapshot[name]:
            channel = row[field]
            if channel in claimed or channel not in locations:
                raise RuntimeError(
                    f"Conflicting/orphan v1 claim: {name} row {row['id']}"
                )
            claimed.add(channel)
            conn.exec_driver_sql(
                f'UPDATE platform_channel_surfaces SET "{column}"=? WHERE id=?',
                (row["id"], locations[channel]["id"]),
            )
    # Existing thread identities provide the parent forum for comment mappings.
    thread_channels = {}
    for name, thread_field, channel_field in [
        (
            "local_community_thread_surfaces",
            "discord_thread_id",
            "discord_forum_channel_id",
        ),
        (
            "community_thread_group_deliveries",
            "discord_thread_id",
            "discord_channel_id",
        ),
        ("post_links", "discord_forum_thread_id", "discord_forum_channel_id"),
        ("personal_feed_thread_mappings", "thread_id", "root_discord_channel_id"),
    ]:
        for row in snapshot[name]:
            thread_id, channel_id = row[thread_field], row[channel_field]
            if channel_id is None:
                raise RuntimeError(f"Orphan legacy thread {name} row {row['id']}")
            if (
                thread_id in thread_channels
                and thread_channels[thread_id] != channel_id
            ):
                raise RuntimeError(f"Ambiguous legacy thread {thread_id}")
            thread_channels[thread_id] = channel_id
    # Resolve replies against the immutable snapshot, scoped to the exact target.
    # Index once so large comment archives do not require repeated linear scans.
    groups_by_id = {
        group["id"]: group for group in snapshot["community_message_groups"]
    }
    deliveries_by_target = {
        (
            delivery["message_group_id"],
            delivery["discord_channel_id"],
            delivery["discord_thread_id"],
        ): delivery
        for delivery in snapshot["community_message_group_deliveries"]
    }
    for name in [n for n in affected if n not in owners]:
        transformed = []
        for original in snapshot[name]:
            row = original.copy()
            is_source = name in {
                "community_thread_groups",
                "community_message_groups",
                "message_mappings",
                "published_activity_objects",
            }
            prefix = "source_" if is_source else ""
            if name == "comment_links":
                channel = thread_channels.get(row["discord_forum_thread_id"])
            else:
                channel = next(
                    (
                        row.get(field)
                        for field in [
                            "discord_forum_channel_id",
                            "root_discord_channel_id",
                            "destination_channel_id",
                            "source_channel_id",
                            "discord_channel_id",
                        ]
                        if row.get(field) is not None
                    ),
                    None,
                )
            if name == "message_mappings" and row["source_platform"] != "discord":
                channel = None
            if channel is None and not is_source:
                raise RuntimeError(
                    f"Orphan required copy location: {name} row {row['id']}"
                )
            row[prefix + "connector_id"] = (
                connector
                if channel is not None
                or (name == "message_mappings" and row["source_platform"] == "discord")
                or (
                    name == "published_activity_objects"
                    and row.get("discord_message_id") is not None
                )
                else None
            )
            row[prefix + "channel_surface_id"] = (
                locations[channel]["id"] if channel is not None else None
            )
            aliases = {
                "external_thread_id": [
                    "discord_thread_id",
                    "discord_forum_thread_id",
                    "thread_id",
                    "destination_thread_id",
                ],
                "external_starter_message_id": [
                    "discord_starter_message_id",
                    "root_discord_message_id",
                ],
                "external_message_id": ["discord_message_id"],
                "external_parent_message_id": ["parent_discord_message_id"],
                "source_external_thread_id": ["source_thread_id"],
                "source_external_message_id": [
                    "source_message_id",
                    "discord_message_id",
                ],
            }
            for field in Base.metadata.tables[name].columns.keys():
                if field in aliases:
                    value = next(
                        (
                            row.get(alias)
                            for alias in aliases[field]
                            if row.get(alias) is not None
                        ),
                        None,
                    )
                    row[field] = str(value) if value is not None else None
            if name == "community_message_group_deliveries":
                message_group = groups_by_id.get(row["message_group_id"])
                if message_group is None:
                    raise RuntimeError(
                        f"Missing message group for delivery {row['id']}"
                    )
                parent_id = message_group.get("parent_message_group_id")
                if parent_id is not None:
                    parent_copy = deliveries_by_target.get(
                        (
                            parent_id,
                            row["discord_channel_id"],
                            row["discord_thread_id"],
                        )
                    )
                    if parent_copy is None:
                        raise RuntimeError(
                            f"Missing target-local parent for message delivery {row['id']}"
                        )
                    row["external_parent_message_id"] = str(
                        parent_copy["discord_message_id"]
                    )
            transformed.append(row)
        _rebuild_v2_table(conn, name, transformed)
    # Compare every original column in every table, excluding only the two
    # intentionally removed actor-placement fields. Descendant rows stay intact.
    for name, original_rows in snapshot.items():
        columns = _table_columns(conn, name)
        common = (
            [column for column in original_rows[0] if column in columns]
            if original_rows
            else []
        )
        if not original_rows:
            if conn.exec_driver_sql(f'SELECT count(*) FROM "{name}"').scalar_one():
                raise RuntimeError(f"Unexpected rows after migration: {name}")
        else:
            actual = [
                tuple(row)
                for row in conn.exec_driver_sql(
                    f'SELECT {",".join(chr(34)+c+chr(34) for c in common)} FROM "{name}" ORDER BY id'
                )
            ]
            expected_rows = [tuple(row[c] for c in common) for row in original_rows]
            if actual != expected_rows:
                raise RuntimeError(f"Migration changed immutable v1 records: {name}")
    _install_current_schema_objects(conn)
    _validate_v2_schema(conn)


DATABASE_VERSION = 2
MIGRATIONS: dict[int, Callable[[Connection], None]] = {2: _upgrade_platform_model_v2}


def _validate_registry() -> None:
    """Reject an incomplete upgrade sequence before bootstrap or migration writes."""
    if type(DATABASE_VERSION) is not int or not 1 <= DATABASE_VERSION <= 2147483647:
        raise RuntimeError("Invalid database migration registry target version")
    # Baseline adoption is not an upgrade function. Every later version must
    # have exactly one callable, even when the stored header is already current.
    if (
        any(
            type(version) is not int or not 2 <= version <= DATABASE_VERSION
            for version in MIGRATIONS
        )
        or len(MIGRATIONS) != DATABASE_VERSION - 1
        or any(not callable(step) for step in MIGRATIONS.values())
    ):
        raise RuntimeError(
            "Invalid database migration registry: expected consecutive callable upgrades"
        )


def _read_version(conn: Connection) -> int:
    """Read and validate the SQLite header before allowing any schema writes."""
    version = int(conn.exec_driver_sql("PRAGMA user_version").scalar_one())
    if version < 0 or version > DATABASE_VERSION:
        raise RuntimeError(
            f"Unsupported database version {version}; application supports {DATABASE_VERSION}"
        )
    return version


def read_database_state(engine: Engine) -> tuple[int, bool]:
    """Preflight the header and detect only absence of user tables for bootstrap.

    This does not inspect ActivityPub rows or prove an unversioned baseline.
    Nonempty unversioned databases are accepted only in the baseline release.
    """
    _validate_registry()
    with engine.connect() as conn:
        _require_sqlite_model_support(conn)
        version = _read_version(conn)
        empty = (
            version == 0
            and conn.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name NOT GLOB 'sqlite_*' LIMIT 1"
            ).first()
            is None
        )
    if version == 0 and not empty and DATABASE_VERSION != 1:
        raise RuntimeError(
            "Unversioned database requires confirmed baseline: back up the database, "
            "verify the typed development baseline, then manually set PRAGMA user_version = 1"
        )
    return version, empty


def stamp_initial_version(engine: Engine) -> None:
    """Stamp successful current-schema bootstrap without lowering a concurrent version.

    Bootstrap and legacy helpers have already succeeded outside this transaction.
    The header assignment itself is atomic; it does not certify historical data.
    """
    _validate_registry()
    with engine.connect() as conn:
        try:
            # SQLAlchemy's logical autobegin does not reserve SQLite's writer.
            # Use a physical BEGIN on a connection with no prior DBAPI work.
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            if _read_version(conn) == 0:
                conn.exec_driver_sql(f"PRAGMA user_version = {DATABASE_VERSION}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    # A concurrent startup may have adopted a lower supported version; never
    # jump over its remaining registered steps by overwriting the header.
    run_pending_migrations(engine)


def run_pending_migrations(engine: Engine) -> None:
    """Run consecutive registered upgrades with atomic DDL, DML and header writes.

    A current header requires no writer reservation. Each pending step rereads
    the header under BEGIN IMMEDIATE, so concurrent runners cannot repeat it.
    Migration functions use the supplied connection and must not commit it.
    """
    _validate_registry()
    with engine.connect() as conn:
        version = _read_version(conn)
    if version == 0:
        raise RuntimeError(
            "Database baseline must be established before registered upgrades"
        )
    if version == DATABASE_VERSION:
        with engine.connect() as conn:
            _require_sqlite_model_support(conn)
            if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                raise RuntimeError("Current database requires foreign_keys=ON")
            _validate_v2_schema(conn)
    while version < DATABASE_VERSION:
        with engine.connect() as conn:
            suspended = False
            try:
                _require_sqlite_model_support(conn)
                # This fresh physical connection has no DBAPI transaction yet.
                # Only v1->v2 parent rebuilds may suspend enforcement.
                if version == 1:
                    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                        raise RuntimeError("Upgrade connection lacks FK enforcement")
                    suspended = True
                    conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
                    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
                        raise RuntimeError(
                            "Could not suspend FK enforcement before upgrade"
                        )
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                version = _read_version(conn)
                if version == 0:
                    raise RuntimeError("Database baseline disappeared before upgrade")
                if version < DATABASE_VERSION:
                    next_version = version + 1
                    MIGRATIONS[next_version](conn)
                    conn.exec_driver_sql(f"PRAGMA user_version = {next_version}")
                    version = next_version
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                # No physical transaction may remain when restoring PRAGMA;
                # failed restoration must never return this connection to pool.
                if suspended:
                    try:
                        conn.rollback()
                        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
                        if (
                            conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
                            != 1
                        ):
                            raise RuntimeError("FK pragma remained disabled")
                    except BaseException as exc:
                        conn.invalidate()
                        raise RuntimeError(
                            "Failed to restore SQLite FK enforcement"
                        ) from exc


def run_legacy_schema_helpers(engine: Engine) -> None:
    """Preserve supported additive helpers separately from registered upgrades.

    Requires Database.create_all() to have created missing current tables first.
    Existing table rebuilds retain their transaction and foreign-key behavior;
    they are outside the runner's per-version atomicity contract.
    """
    migrations = [
        (
            "channel_community_subscriptions",
            "discord_guild_id",
            "ALTER TABLE channel_community_subscriptions ADD COLUMN discord_guild_id INTEGER",
        ),
        (
            "local_communities",
            "created_by_discord_user_id",
            "ALTER TABLE local_communities ADD COLUMN created_by_discord_user_id VARCHAR(64)",
        ),
        (
            "activitypub_event_receipts",
            "outcome",
            "ALTER TABLE activitypub_event_receipts ADD COLUMN outcome VARCHAR(64)",
        ),
        (
            "users",
            "display_name",
            "ALTER TABLE users ADD COLUMN display_name VARCHAR(30)",
        ),
        ("users", "summary", "ALTER TABLE users ADD COLUMN summary VARCHAR(500)"),
        (
            "users",
            "avatar_media_id",
            "ALTER TABLE users ADD COLUMN avatar_media_id INTEGER REFERENCES profile_media_assets(id) ON DELETE RESTRICT",
        ),
        (
            "users",
            "header_media_id",
            "ALTER TABLE users ADD COLUMN header_media_id INTEGER REFERENCES profile_media_assets(id) ON DELETE RESTRICT",
        ),
        (
            "users",
            "profile_version",
            "ALTER TABLE users ADD COLUMN profile_version INTEGER NOT NULL DEFAULT 0",
        ),
    ]
    with engine.connect() as conn:
        _migrate_published_activity_objects_stage5(conn)
        _migrate_message_mappings_stage7(conn)
        for table, column, stmt in migrations:
            # PRAGMA table_info returns one row per column; skip if already present.
            existing = _table_columns(conn, table)
            # Partial legacy fixtures may omit unrelated tables entirely. A
            # missing table has nothing to migrate and may be created later by
            # the normal metadata path.
            if not existing:
                continue
            if column not in existing:
                conn.execute(text(stmt))
        if "conversation_root_object_id" in _table_columns(
            conn, "published_activity_objects"
        ):
            conn.execute(
                text(
                    "UPDATE published_activity_objects SET conversation_root_object_id = "
                    "CASE WHEN kind = 'post' THEN object_id ELSE COALESCE(in_reply_to_object_id, object_id) END "
                    "WHERE conversation_root_object_id IS NULL"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_published_activity_objects_conversation_root_object_id "
                    "ON published_activity_objects (conversation_root_object_id)"
                )
            )
        _migrate_local_communities_summary_nullable(conn)
        _migrate_generalized_user_bans(conn)
        # Stage 3 stores the selected LocalSubscriber on subscriber surface rows.
        # Existing host-only surface rows keep NULL, so this additive migration is
        # safe to run before or after Stage 2 backfill.
        for table in (
            "local_community_thread_surfaces",
            "local_community_message_surfaces",
        ):
            columns = _table_columns(conn, table)
            if "local_subscriber_id" not in columns:
                conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN local_subscriber_id INTEGER")
                )
        _verify_stage2_surface_invariants(conn)
        _migrate_profile_field_name_width(conn)
        _migrate_user_actor_outbound_sources_stage5(conn)
        _install_profile_media_triggers(conn)
        # Partial legacy fixtures may not contain authored-object storage yet.
        if _table_columns(conn, "published_activity_objects"):
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_published_objects_user_outbox "
                    "ON published_activity_objects "
                    "(actor_username, actor_url, deleted_at, published_at, id)"
                )
            )
        conn.commit()


def _migrate_published_activity_objects_stage5(conn: Connection) -> None:
    """Rebuild authored objects to the current typed publication-context schema."""
    columns = _table_columns(conn, "published_activity_objects")
    required = {column.name for column in PublishedActivityObject.__table__.columns}
    if not columns or required.issubset(columns):
        return

    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.execute(text("PRAGMA legacy_alter_table=ON"))
    conn.execute(
        text(
            "ALTER TABLE published_activity_objects RENAME TO published_activity_objects_legacy"
        )
    )
    conn.execute(text("PRAGMA legacy_alter_table=OFF"))
    PublishedActivityObject.__table__.create(conn)
    optional = {
        "content_revision": "1",
        "deleted_at": "NULL",
        "desired_kind": "'none'",
        "desired_title": "NULL",
        "desired_body_markdown": "NULL",
        "desired_edited_timestamp": "NULL",
        "desired_fingerprint": "NULL",
        "discord_reconciliation_needed": "0",
        "discord_reconciliation_attempt_count": "0",
        "discord_reconciliation_next_attempt_at": "NULL",
        "discord_reconciliation_last_error": "NULL",
    }

    def expr(name: str) -> str:
        return name if name in columns else optional[name]

    conn.execute(text(f"""
        INSERT INTO published_activity_objects (
            id, actor_username, actor_url, publication_context, community_actor_url,
            activity_id, object_id, kind, title, body_markdown, in_reply_to_object_id,
            discord_guild_id, discord_channel_id, discord_message_id, content_revision,
            deleted_at, desired_kind, desired_title, desired_body_markdown,
            desired_edited_timestamp, desired_fingerprint, discord_reconciliation_needed,
            discord_reconciliation_attempt_count, discord_reconciliation_next_attempt_at,
            discord_reconciliation_last_error, published_at, created_at
        )
        SELECT id, actor_username, actor_url, 'community', community_actor_url,
               activity_id, object_id, kind, title, body_markdown, in_reply_to_object_id,
               NULL, discord_channel_id, discord_message_id, {expr("content_revision")},
               {expr("deleted_at")}, {expr("desired_kind")}, {expr("desired_title")},
               {expr("desired_body_markdown")}, {expr("desired_edited_timestamp")},
               {expr("desired_fingerprint")}, {expr("discord_reconciliation_needed")},
               {expr("discord_reconciliation_attempt_count")},
               {expr("discord_reconciliation_next_attempt_at")},
               {expr("discord_reconciliation_last_error")}, published_at, created_at
        FROM published_activity_objects_legacy
    """))
    conn.execute(text("DROP TABLE published_activity_objects_legacy"))
    conn.execute(text("PRAGMA foreign_keys=ON"))


def _migrate_message_mappings_stage7(conn: Connection) -> None:
    """Rebuild mappings with typed context and durable Discord guild identity."""
    columns = _table_columns(conn, "message_mappings")
    required = {column.name for column in MessageMapping.__table__.columns}
    if not columns or required.issubset(columns):
        return
    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.execute(text("ALTER TABLE message_mappings RENAME TO message_mappings_legacy"))
    MessageMapping.__table__.create(conn)
    conn.execute(text("""
        INSERT INTO message_mappings (
            id, source_platform, source_id, activity_id, object_id, actor_url,
            publication_context, community_actor_url, discord_guild_id,
            discord_channel_id, discord_message_id, created_at
        )
        SELECT id, source_platform, source_id, activity_id, object_id, actor_url,
               'community', community_actor_url, NULL, discord_channel_id,
               discord_message_id, created_at
        FROM message_mappings_legacy
    """))
    conn.execute(text("DROP TABLE message_mappings_legacy"))
    conn.execute(text("PRAGMA foreign_keys=ON"))


def _migrate_user_actor_outbound_sources_stage5(conn: Connection) -> None:
    """Generalize Stage 4 profile sources without losing retry state."""
    columns = _table_columns(conn, "user_actor_outbound_sources")
    if not columns or "snapshot_json" in columns:
        return
    conn.execute(text("PRAGMA foreign_keys=OFF"))
    # SQLite normally rewrites child-table foreign-key SQL during RENAME even
    # while enforcement is disabled.  Preserve the Stage 4 child references to
    # the canonical table name while this parent table is rebuilt.
    conn.execute(text("PRAGMA legacy_alter_table=ON"))
    conn.execute(
        text(
            "ALTER TABLE user_actor_outbound_sources RENAME TO user_actor_outbound_sources_stage4"
        )
    )
    conn.execute(text("PRAGMA legacy_alter_table=OFF"))
    conn.execute(text("DROP INDEX IF EXISTS ix_user_actor_sources_retry"))
    UserActorOutboundSource.__table__.create(conn)
    conn.execute(text("""
        INSERT INTO user_actor_outbound_sources (
            id, user_id, source_kind, ordering_key, logical_version, activity_id,
            published_object_id, snapshot_json, activity_json, state,
            render_attempt_count, last_render_attempted_at, last_error,
            next_attempt_at, created_at, updated_at
        )
        SELECT id, user_id, source_kind, 'profile:' || user_id, logical_version,
               activity_id, NULL, profile_snapshot_json, activity_json, state,
               render_attempt_count, last_render_attempted_at, last_error,
               next_attempt_at, created_at, updated_at
        FROM user_actor_outbound_sources_stage4
        """))
    conn.execute(text("DROP TABLE user_actor_outbound_sources_stage4"))
    conn.execute(text("PRAGMA foreign_keys=ON"))


def _migrate_generalized_user_bans(conn: Connection) -> None:
    """Rebuild legacy community-only ban rows into explicit scoped rows.

    SQLite cannot make ``local_community_id`` nullable or replace the legacy
    uniqueness constraint in place. Existing rows remain community-scoped and
    retain status, reason, actor identity, creator, and timestamps.
    """
    columns = _table_columns(conn, "community_actor_bans")
    if not columns or "scope" in columns:
        return
    conn.execute(
        text(
            "ALTER TABLE community_actor_bans RENAME TO community_actor_bans_legacy_scope"
        )
    )
    conn.execute(text("""
        CREATE TABLE community_actor_bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope VARCHAR(32) NOT NULL,
            scope_key VARCHAR(64) NOT NULL,
            local_community_id INTEGER,
            actor_handle VARCHAR(255) NOT NULL,
            actor_url VARCHAR(512),
            target_discord_user_id VARCHAR(64),
            status VARCHAR(32) NOT NULL,
            created_by_discord_user_id VARCHAR(64),
            reason VARCHAR(1024),
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            UNIQUE (scope, scope_key, actor_handle, status)
        )
        """))
    conn.execute(text("""
        INSERT INTO community_actor_bans (
            id, scope, scope_key, local_community_id, actor_handle, actor_url,
            target_discord_user_id, status, created_by_discord_user_id, reason,
            created_at, updated_at
        )
        SELECT id, 'community', CAST(local_community_id AS TEXT),
               local_community_id, actor_handle, actor_url, NULL, status,
               created_by_discord_user_id, reason, created_at, updated_at
        FROM community_actor_bans_legacy_scope
        """))
    conn.execute(text("DROP TABLE community_actor_bans_legacy_scope"))
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_community_actor_bans_target_discord_user_id "
            "ON community_actor_bans (target_discord_user_id)"
        )
    )


def _table_columns(conn: Connection, table: str) -> set[str]:
    """Return the current SQLite column names for one table."""
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {str(row[1]) for row in rows}


def _table_info(conn: Connection, table: str) -> list[object]:
    """Return raw SQLite table-info rows for one table."""
    return list(conn.execute(text(f"PRAGMA table_info({table})")).fetchall())


def _migrate_local_communities_summary_nullable(conn: Connection) -> None:
    """Rebuild `local_communities` when legacy schema has `summary NOT NULL`.

    SQLite cannot drop a NOT NULL constraint with `ALTER TABLE`. The rebuild is
    intentionally limited to this table and recreates the same identity
    constraints the SQLAlchemy model declares. Existing summary values are
    copied unchanged, while future rows may store NULL.
    """
    table_info = _table_info(conn, "local_communities")
    if not table_info:
        return
    summary_row = next((row for row in table_info if row[1] == "summary"), None)
    if summary_row is None or int(summary_row[3]) == 0:
        return

    if int(conn.exec_driver_sql("PRAGMA user_version").scalar_one()) >= 2:
        # V2 actor placement belongs to ChannelSurface. A legacy rebuild must
        # never resurrect removed Discord columns on a stamped current schema.
        raise RuntimeError("V2 LocalCommunity.summary must already be nullable")

    existing_columns = {str(row[1]) for row in table_info}
    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.execute(
        text(
            "ALTER TABLE local_communities RENAME TO local_communities_legacy_summary_not_null"
        )
    )
    conn.execute(text("""
            CREATE TABLE local_communities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_guild_id INTEGER NOT NULL,
                discord_forum_channel_id INTEGER NOT NULL,
                slug VARCHAR(255) NOT NULL,
                display_name VARCHAR(255) NOT NULL,
                summary VARCHAR,
                created_by_discord_user_id VARCHAR(64),
                actor_url VARCHAR(512) NOT NULL,
                inbox_url VARCHAR(512) NOT NULL,
                outbox_url VARCHAR(512) NOT NULL,
                followers_url VARCHAR(512) NOT NULL,
                public_key_pem VARCHAR NOT NULL,
                private_key_pem VARCHAR NOT NULL,
                status VARCHAR(32) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE (discord_forum_channel_id),
                UNIQUE (slug),
                UNIQUE (actor_url)
            )
            """))

    created_by_expr = (
        "created_by_discord_user_id"
        if "created_by_discord_user_id" in existing_columns
        else "NULL"
    )
    # Keep the column list explicit so future table changes do not get silently
    # dropped into this compatibility path without a deliberate migration plan.
    conn.execute(text(f"""
            INSERT INTO local_communities (
                id,
                discord_guild_id,
                discord_forum_channel_id,
                slug,
                display_name,
                summary,
                created_by_discord_user_id,
                actor_url,
                inbox_url,
                outbox_url,
                followers_url,
                public_key_pem,
                private_key_pem,
                status,
                created_at,
                updated_at
            )
            SELECT
                id,
                discord_guild_id,
                discord_forum_channel_id,
                slug,
                display_name,
                summary,
                {created_by_expr},
                actor_url,
                inbox_url,
                outbox_url,
                followers_url,
                public_key_pem,
                private_key_pem,
                status,
                created_at,
                updated_at
            FROM local_communities_legacy_summary_not_null
            """))
    conn.execute(text("DROP TABLE local_communities_legacy_summary_not_null"))
    conn.execute(text("PRAGMA foreign_keys=ON"))


def _verify_stage2_surface_invariants(conn: Connection) -> None:
    """Fail loudly when Stage 2 host-surface ownership is ambiguous."""
    thread_violations = conn.execute(text("""
            SELECT thread.id
            FROM local_community_threads AS thread
            LEFT JOIN local_community_thread_surfaces AS surface
              ON surface.local_community_thread_id = thread.id
             AND surface.role = 'host'
            GROUP BY thread.id
            HAVING COUNT(surface.id) != 1
            """)).fetchall()
    if thread_violations:
        raise RuntimeError(
            "Stage 2 migration requires exactly one host thread surface per canonical thread"
        )

    message_violations = conn.execute(text("""
            SELECT message.id
            FROM local_community_messages AS message
            LEFT JOIN local_community_message_surfaces AS surface
              ON surface.local_community_message_id = message.id
             AND surface.role = 'host'
            GROUP BY message.id
            HAVING COUNT(surface.id) != 1
            """)).fetchall()
    if message_violations:
        raise RuntimeError(
            "Stage 2 migration requires exactly one host message surface per canonical message"
        )


def _migrate_profile_field_name_width(conn: Connection) -> None:
    """Rebuild legacy profile fields so metadata names expose VARCHAR(255)."""
    table_info = _table_info(conn, "user_profile_fields")
    if not table_info:
        return
    name_row = next((row for row in table_info if row[1] == "name"), None)
    if name_row is None or str(name_row[2]).upper() != "VARCHAR(30)":
        return
    # SQLite does not enforce VARCHAR widths, but schema parity is part of the
    # durable contract handed to later stages and external schema inspection.
    conn.execute(
        text(
            "ALTER TABLE user_profile_fields RENAME TO user_profile_fields_legacy_name_width"
        )
    )
    conn.execute(text("""
        CREATE TABLE user_profile_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            name VARCHAR(255) NOT NULL,
            value VARCHAR(255) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            UNIQUE (user_id, position),
            CONSTRAINT ck_user_profile_field_position CHECK (position >= 0 AND position <= 3)
        )
    """))
    conn.execute(text("""
        INSERT INTO user_profile_fields (id, user_id, position, name, value, created_at, updated_at)
        SELECT id, user_id, position, name, value, created_at, updated_at
        FROM user_profile_fields_legacy_name_width
    """))
    conn.execute(text("DROP TABLE user_profile_fields_legacy_name_width"))


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
