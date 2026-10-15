"""Frozen Stage01 historical fixture oracle; never an application migration route.

These tests deliberately exercise the unsupported 43-table development shape,
including features absent from released v0. Production startup rejects that
shape. The captured 146 test oracle retains those cascade regressions without
keeping an obsolete runtime fallback or intermediate-schema production DDL.
"""
from sqlalchemy import text
from sqlalchemy.engine import Connection
from src.db import migrations
from src.db.migrations import (_require_sqlite_model_support, _table_columns, _install_current_schema_objects, _validate_current_v1_schema)


def _rebuild_current_table(conn: Connection, name: str, rows: list[dict]) -> None:
    """Replace one approved table while retaining original IDs and explicit schema objects.

    Called only by the FK-suspended current upgrade. Copy DML excludes computed
    columns and preserves stored values without datetime/JSON reinterpretation.
    """
    from sqlalchemy.schema import CreateTable, CreateIndex
    from src.models import Base

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


def _historical_reply_indexes(snapshot: dict) -> tuple[dict, dict]:
    """Validate historical group/reply targets once, before any placement DDL.

    Both released recognition and the private platform phase have the same
    historical identity fields. Sharing this boundary prevents divergent rules.
    """
    groups = {g["id"]: g for g in snapshot["community_message_groups"]}
    deliveries = {
        (d["message_group_id"], d["discord_channel_id"], d["discord_thread_id"]): d
        for d in snapshot["community_message_group_deliveries"]
    }
    finished = set()
    for group in groups.values():
        parent = groups.get(group["parent_message_group_id"])
        if group["parent_message_group_id"] is not None and parent is None:
            raise RuntimeError(
                f"Missing legacy parent: message group row {group['id']}"
            )
        if parent is not None and (
            parent["thread_group_id"] != group["thread_group_id"]
            or parent["community_actor_id"] != group["community_actor_id"]
        ):
            raise RuntimeError(
                f"Conflicting legacy parent thread: message group row {group['id']}"
            )
        chain = set()
        current = group["id"]
        while current is not None and current not in finished:
            if current in chain:
                raise RuntimeError(f"Cyclic legacy parent: message group row {current}")
            chain.add(current)
            current = (
                groups[current]["parent_message_group_id"]
                if current in groups
                else None
            )
        finished.update(chain)
    for row in snapshot["community_message_group_deliveries"]:
        group = groups.get(row["message_group_id"])
        if group is None:
            raise RuntimeError(f"Missing message group for delivery {row['id']}")
        parent_id = group["parent_message_group_id"]
        if (
            parent_id is not None
            and (parent_id, row["discord_channel_id"], row["discord_thread_id"])
            not in deliveries
        ):
            raise RuntimeError(
                f"Missing target-local parent for message delivery {row['id']}"
            )
    return groups, deliveries


def _upgrade_historical_platform_fixture(conn: Connection) -> None:
    """Backfill normalized historical platform locations without changing canonical identities.

    Runner owns FK suspension and the physical transaction. Every baseline
    row is snapshotted before DDL and compared on its unchanged columns before
    the runner can stamp version 1; malformed origins or ownership fail closed.
    """
    from datetime import datetime, timezone
    from src.models import Base, PlatformConnector, ChannelSurface

    _require_sqlite_model_support(conn)
    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError(
            "Current v1 parent rebuild requires pre-transaction FK suspension"
        )
    if conn.exec_driver_sql("PRAGMA foreign_key_check").all():
        raise RuntimeError("Historical input contains foreign-key violations")
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
            f"Unsupported historical input schema tables: missing={sorted(expected-names)}, extra={sorted(names-expected)}"
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
    # Known actor/domain tables have no historical FK dependents. Published objects
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
                    f"Unexpected inbound historical FK: {name}.{fk[3]} -> {fk[2]}"
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
    # Fail on partial upgrades or missing baseline fields before creating current v1.
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
                f"Unsupported or partially normalized historical input columns in {name}"
            )
    for name, required in [
        ("local_subscribers", {"discord_channel_id"}),
        ("channel_community_subscriptions", {"discord_channel_id"}),
        ("personal_feed_bindings", {"discord_channel_id", "discord_guild_id"}),
    ]:
        info = {r[1]: r for r in _table_info(conn, name)}
        if any(info[column][3] != 1 for column in required):
            raise RuntimeError(f"Partially normalized historical owner table: {name}")
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
    groups_by_id, deliveries_by_target = _historical_reply_indexes(snapshot)
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
        _rebuild_current_table(conn, name, snapshot[name])
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
                    f"Conflicting/orphan historical claim: {name} row {row['id']}"
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
                message_group = groups_by_id[row["message_group_id"]]
                parent_id = message_group.get("parent_message_group_id")
                if parent_id is not None:
                    parent_copy = deliveries_by_target[
                        (parent_id, row["discord_channel_id"], row["discord_thread_id"])
                    ]
                    row["external_parent_message_id"] = str(
                        parent_copy["discord_message_id"]
                    )
            transformed.append(row)
        _rebuild_current_table(conn, name, transformed)
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
                raise RuntimeError(
                    f"Migration changed immutable historical input records: {name}"
                )
    _install_current_schema_objects(conn)
    _validate_current_v1_schema(conn)


def _table_info(conn: Connection, table: str) -> list[object]:
    """Return raw SQLite table-info rows for one table."""
    return list(conn.execute(text(f"PRAGMA table_info({table})")).fetchall())


def upgrade_historical_probe(database):
    """Exercise the private platform phase and its real cascade graph transactionally.

    Public startup rejects this unpublished input shape. Only tests deliberately
    constructing the captured internal shape call this helper; ready targets and
    fresh files use the real application runner.
    """
    with database.engine.connect() as conn:
        names = migrations._user_tables(conn)
    if "personal_feed_bindings" not in names or "platform_connectors" in names:
        database.migrate()
        return
    with database.engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.rollback()
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            _upgrade_historical_platform_fixture(conn)
            conn.exec_driver_sql("PRAGMA user_version=1")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            migrations._restore_foreign_keys(conn)
    database.migrate()
