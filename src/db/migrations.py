"""Atomic released-v0 upgrades and current-v1 validation for bridge SQLite.

The runner owns the writer lock, header and FK suspension. Historical
normalization and platform backfill share its single physical transaction.
Each inspection captures its own source evidence; the writer passes one immutable
snapshot through validation, identity planning and historical normalization.
"""

from __future__ import annotations

from collections.abc import Callable
import re

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from .legacy_identity_cutover import (
    IdentityCutoverConfig,
    plan_identity_cutover,
    apply_identity_cutover,
    validate_current_identity,
)


def _normalized_sql(sql):
    """Normalize incidental whitespace and identifier quoting, preserving literals."""
    tokens = re.findall(
        r"'(?:''|[^'])*'|\"[^\"]*\"|`[^`]*`|\[[^\]]*\]|\w+|[^\s]", sql or ""
    )
    return " ".join(t if t.startswith("'") else t.strip('"`[]').lower() for t in tokens)


def _column_definition(ddl: str, name: str) -> str | None:
    """Extract one complete table column clause, respecting nested and quoted SQL.

    Commas/parentheses inside generated expressions or string literals must not
    truncate the clause; checking the entire clause catches appended operators.
    """
    tokens = re.findall(
        r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|\w+|[^\s]", ddl
    )
    depth = 0
    clause = []
    for token in tokens:
        if token == "(":
            depth += 1
            if depth == 1:
                continue
        elif token == ")":
            depth -= 1
        if depth == 0 or token == "," and depth == 1:
            if clause and _normalized_sql(clause[0]) == _normalized_sql(name):
                return _normalized_sql(" ".join(clause))
            clause = []
        elif depth > 0:
            clause.append(token)
    return None


def _checks(sql):
    """Extract balanced CHECK expressions without depending on constraint names."""
    result = []
    for m in re.finditer(r"\bCHECK\s*\(", sql or "", re.I):
        start = m.end()
        depth = 1
        quote = None
        i = start
        while depth:
            c = sql[i]
            if quote:
                if c == quote:
                    if i + 1 < len(sql) and sql[i + 1] == quote:
                        i += 1
                    else:
                        quote = None
            elif c in "'\"":
                quote = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        result.append(_normalized_sql(sql[start : i - 1]))
    return tuple(sorted(result))


def _table_manifest(conn: Connection, name: str) -> dict:
    """Record semantic columns, FK groups, uniqueness, checks and explicit objects."""

    def rows(sql):
        """Read schema evidence through the runner-owned connection."""
        return conn.exec_driver_sql(sql).all()

    quote = '"' + name.replace('"', '""') + '"'
    cols = tuple(
        sorted(
            (
                r[1],
                r[2].upper(),
                r[3],
                _normalized_sql(r[4]) if r[4] is not None else None,
                r[5],
                r[6],
            )
            for r in rows("PRAGMA table_xinfo(" + quote + ")")
        )
    )
    groups = {}
    for r in rows("PRAGMA foreign_key_list(" + quote + ")"):
        groups.setdefault(r[0], []).append(r)
    fks = tuple(
        sorted(
            (
                g[0][2],
                tuple(r[3] for r in sorted(g, key=lambda r: r[1])),
                tuple(r[4] for r in sorted(g, key=lambda r: r[1])),
                g[0][5],
                g[0][6],
                g[0][7],
            )
            for g in groups.values()
        )
    )
    indexes = []
    unique = []
    for r in rows("PRAGMA index_list(" + quote + ")"):
        columns = tuple(x[2] for x in rows('PRAGMA index_info("' + r[1] + '")'))
        if r[2]:
            unique.append(columns)
        if r[3] == "c":
            sql = rows("SELECT sql FROM sqlite_master WHERE name='" + r[1] + "'")[0][0]
            indexes.append((r[1], _normalized_sql(sql)))
    ddl = rows(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='" + name + "'"
    )[0][0]
    triggers = tuple(
        sorted(
            (r[0], _normalized_sql(r[1]))
            for r in rows(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='"
                + name
                + "'"
            )
        )
    )
    return {
        "columns": cols,
        "fks": fks,
        "unique": tuple(sorted(unique)),
        "checks": _checks(ddl),
        "indexes": tuple(sorted(indexes)),
        "triggers": triggers,
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


def _model_table_manifest(conn: Connection, table) -> dict:
    """Build the target semantic contract from current metadata, not a persistent snapshot."""
    from sqlalchemy import UniqueConstraint
    from sqlalchemy.schema import CreateTable, CreateIndex

    primary = list(table.primary_key.columns)
    columns = tuple(
        sorted(
            (
                c.name,
                c.type.compile(dialect=conn.dialect).upper(),
                int(not c.nullable),
                (
                    _normalized_sql(str(c.server_default.arg))
                    if c.server_default is not None and c.computed is None
                    else None
                ),
                primary.index(c) + 1 if c in primary else 0,
                3 if c.computed is not None and c.computed.persisted else 0,
            )
            for c in table.columns
        )
    )
    fks = tuple(
        sorted(
            (
                fk.elements[0].column.table.name,
                tuple(e.parent.name for e in fk.elements),
                tuple(e.column.name for e in fk.elements),
                fk.onupdate or "NO ACTION",
                fk.ondelete or "NO ACTION",
                fk.match or "NONE",
            )
            for fk in table.foreign_key_constraints
        )
    )
    uniques = tuple(
        sorted(
            tuple(c.name for c in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        )
    )
    indexes = tuple(
        sorted(
            (
                index.name,
                _normalized_sql(str(CreateIndex(index).compile(dialect=conn.dialect))),
            )
            for index in table.indexes
        )
    )
    return dict(
        columns=columns,
        fks=fks,
        unique=uniques,
        checks=_checks(str(CreateTable(table).compile(dialect=conn.dialect))),
        indexes=indexes,
        triggers=(),
    )


def _validate_current_v1_schema(conn: Connection) -> None:
    """Validate all target columns, constraints, indexes and triggers without repairing them."""
    from ..models import Base

    if _user_tables(conn) != set(Base.metadata.tables):
        raise RuntimeError(
            "Not the current v1 schema (obsolete development/partial database); restore a supported release backup"
        )
    if conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='view'").first():
        raise RuntimeError("Unexpected current v1 schema view")
    # These deployment objects intentionally supplement model-owned indexes.
    extras = {
        "published_activity_objects": (
            (
                "ix_published_activity_objects_conversation_root_object_id",
                _normalized_sql(
                    "CREATE INDEX ix_published_activity_objects_conversation_root_object_id ON published_activity_objects (conversation_root_object_id)"
                ),
            ),
            (
                "ix_published_objects_user_outbox",
                _normalized_sql(
                    "CREATE INDEX ix_published_objects_user_outbox ON published_activity_objects (actor_username,actor_url,deleted_at,published_at,id)"
                ),
            ),
        )
    }
    for table in Base.metadata.tables.values():
        expected = _model_table_manifest(conn, table)
        expected["indexes"] = tuple(
            sorted(dict(expected["indexes"] + extras.get(table.name, ())).items())
        )
        if table.name == "users":
            triggers = []
            for slot in ("avatar", "header"):
                for action in ("insert", "update"):
                    name = f"users_{slot}_media_kind_{action}"
                    operation = (
                        "INSERT" if action == "insert" else f"UPDATE OF {slot}_media_id"
                    )
                    sql = f"CREATE TRIGGER {name} BEFORE {operation} ON users WHEN NEW.{slot}_media_id IS NOT NULL BEGIN SELECT CASE WHEN (SELECT media_kind FROM profile_media_assets WHERE id = NEW.{slot}_media_id) != '{slot}' THEN RAISE(ABORT, '{slot}_media_id must reference {slot} media') END; END"
                    triggers.append((name, _normalized_sql(sql)))
            expected["triggers"] = tuple(sorted(triggers))
        actual = _table_manifest(conn, table.name)
        for boundary, evidence in expected.items():
            if actual[boundary] != evidence:
                raise RuntimeError(
                    f"Not the current v1 schema: {table.name}.{boundary}; restore a verified backup"
                )
    ddl = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE name='platform_channel_surfaces'"
    ).scalar_one()
    from sqlalchemy.schema import CreateColumn

    # table_xinfo proves STORED (hidden=3); the full column definition also
    # proves the exact expression and its boundary, not merely a CASE substring.
    role = Base.metadata.tables["platform_channel_surfaces"].c.role
    expected_role = _normalized_sql(
        str(CreateColumn(role).compile(dialect=conn.dialect))
    )
    if _column_definition(ddl, "role") != expected_role:
        raise RuntimeError(
            "Current v1 generated role expression differs from the model"
        )
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


def _rebuild_current_table(conn: Connection, name: str, rows: list[dict]) -> None:
    """Replace one approved table while retaining original IDs and explicit schema objects.

    Called only by the FK-suspended current upgrade. Copy DML excludes computed
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


def _upgrade_platform_model_from_legacy_input(conn: Connection) -> None:
    """Backfill normalized historical platform locations without changing canonical identities.

    Runner owns FK suspension and the physical transaction. Every baseline
    row is snapshotted before DDL and compared on its unchanged columns before
    the runner can stamp version 1; malformed origins or ownership fail closed.
    """
    from datetime import datetime, timezone
    from ..models import Base, PlatformConnector, ChannelSurface

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


def _validate_schema_manifest(conn: Connection, expected: dict, label: str) -> None:
    """Reject semantic source differences rather than trusting a table count or SQL hash."""
    actual = _user_tables(conn)
    if actual != set(expected):
        raise RuntimeError(
            f"Unsupported {label} tables: missing={sorted(set(expected)-actual)}, extra={sorted(actual-set(expected))}"
        )
    if conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='view'").first():
        raise RuntimeError(f"Unexpected view in {label}")
    for name, manifest in expected.items():
        observed = _table_manifest(conn, name)
        for boundary, evidence in manifest.items():
            if observed[boundary] != evidence:
                raise RuntimeError(f"Unsupported {label} schema: {name}.{boundary}")
    _validate_sqlite_integrity(conn, label)


def _validate_sqlite_integrity(conn: Connection, label: str) -> None:
    """Check structural and row-level integrity without logging sensitive row contents."""
    violations = conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    if violations:
        raise RuntimeError(f"{label} foreign-key violations: {violations}")
    if conn.exec_driver_sql("PRAGMA integrity_check").scalars().all() != ["ok"]:
        raise RuntimeError(f"{label} SQLite integrity check failed")


def _validate_legacy_release_v0(conn: Connection) -> dict:
    """Recognize released v0.1.0 and return validated source evidence before any DDL."""
    from .legacy_stage00_ddl import RELEASE_MANIFEST

    if conn.exec_driver_sql("PRAGMA user_version").scalar_one() != 0:
        raise RuntimeError("Released legacy source requires version 0")
    _validate_schema_manifest(conn, RELEASE_MANIFEST, "released v0.1.0")
    snapshot = _capture_legacy_identity_and_row_evidence(conn)
    _validate_legacy_relationships(snapshot)
    return snapshot


def _validate_legacy_relationships(rows: dict) -> None:
    """Reject contradictory old ownership and parent identities before normalization DDL.

    Released scalar references were not all SQL FKs. Schema recognition therefore
    checks their domain relationships too, using only evidence already in v0.
    """
    indexed = {name: {r["id"]: r for r in values} for name, values in rows.items()}
    owners = {}
    guilds = {}
    for name, channel_field in (
        ("local_communities", "discord_forum_channel_id"),
        ("local_subscribers", "discord_channel_id"),
        ("channel_community_subscriptions", "discord_channel_id"),
    ):
        for row in rows[name]:
            channel, guild = row[channel_field], row.get("discord_guild_id")
            if channel <= 0 or guild is not None and guild <= 0:
                raise RuntimeError(f"Invalid legacy location: {name} row {row['id']}")
            if channel in owners:
                raise RuntimeError(
                    f"Conflicting legacy owners: {owners[channel]} and {name} row {row['id']}"
                )
            owners[channel] = f"{name} row {row['id']}"
            if guild is not None:
                guilds[channel] = guild
    for name, values in rows.items():
        for row in values:
            channel = row.get("discord_channel_id", row.get("discord_forum_channel_id"))
            guild = row.get("discord_guild_id")
            if channel is not None and guild is not None:
                if channel in guilds and guilds[channel] != guild:
                    raise RuntimeError(
                        f"Conflicting legacy guild: {name} row {row['id']} channel {channel}"
                    )
                guilds[channel] = guild
    references = (
        ("local_subscribers", "local_community_id", "local_communities"),
        ("remote_subscribers", "local_community_id", "local_communities"),
        ("local_community_threads", "local_community_id", "local_communities"),
        (
            "local_community_messages",
            "local_community_thread_id",
            "local_community_threads",
        ),
        (
            "local_community_thread_surfaces",
            "local_community_thread_id",
            "local_community_threads",
        ),
        ("local_community_thread_surfaces", "local_subscriber_id", "local_subscribers"),
        (
            "local_community_message_surfaces",
            "local_community_message_id",
            "local_community_messages",
        ),
        (
            "local_community_message_surfaces",
            "local_community_thread_surface_id",
            "local_community_thread_surfaces",
        ),
        ("community_message_groups", "thread_group_id", "community_thread_groups"),
        (
            "community_message_groups",
            "parent_message_group_id",
            "community_message_groups",
        ),
        (
            "community_thread_group_deliveries",
            "thread_group_id",
            "community_thread_groups",
        ),
        (
            "community_message_group_deliveries",
            "message_group_id",
            "community_message_groups",
        ),
    )
    for name, field, target in references:
        for row in rows[name]:
            if row[field] is not None and row[field] not in indexed[target]:
                raise RuntimeError(
                    f"Missing legacy parent: {name} row {row['id']} {field}={row[field]}"
                )
    _historical_reply_indexes(rows)
    for row in rows["local_community_message_surfaces"]:
        message = indexed["local_community_messages"][row["local_community_message_id"]]
        thread = indexed["local_community_thread_surfaces"][
            row["local_community_thread_surface_id"]
        ]
        if (
            message["local_community_thread_id"] != thread["local_community_thread_id"]
            or row["discord_forum_channel_id"] != thread["discord_forum_channel_id"]
        ):
            raise RuntimeError(
                f"Conflicting legacy message/thread channel: surface row {row['id']}"
            )


def _capture_legacy_identity_and_row_evidence(conn: Connection) -> dict:
    """Snapshot original columns/values by stable primary key for final preservation."""
    return {
        name: [
            dict(row)
            for row in conn.exec_driver_sql(
                f'SELECT * FROM "{name}" ORDER BY id'
            ).mappings()
        ]
        for name in sorted(_user_tables(conn))
    }


def _normalize_released_v0_to_platform_input(conn: Connection, originals: dict) -> None:
    """Build only historically required input tables/contexts on the caller's connection.

    Pinned DDL is migration-only. Neither today's ORM nor a commit-capable legacy
    helper may manufacture the intermediate shape or expose it to other starters.
    Conversion copies rows; the caller's original source evidence stays immutable.
    """
    from .legacy_stage00_ddl import NORMALIZATION_DDL, INPUT_MANIFEST

    for name, statements in NORMALIZATION_DDL.items():
        if name not in originals:
            for sql in statements:
                conn.exec_driver_sql(sql)
            continue
        replacement = "_new_" + name
        conn.exec_driver_sql(
            statements[0].replace(
                "CREATE TABLE " + name, "CREATE TABLE " + replacement, 1
            )
        )
        columns = [
            r[1] for r in conn.exec_driver_sql(f'PRAGMA table_xinfo("{replacement}")')
        ]
        converted = []
        for original in originals[name]:
            row = original.copy()
            if name == "users":
                row["profile_version"] = 0
            else:
                row["publication_context"] = "community"
                if name == "published_activity_objects":
                    row.update(
                        content_revision=1,
                        desired_kind="none",
                        discord_reconciliation_needed=0,
                        discord_reconciliation_attempt_count=0,
                    )
                    row["conversation_root_object_id"] = (
                        row["object_id"]
                        if row["kind"] == "post"
                        else row.get("in_reply_to_object_id") or row["object_id"]
                    )
            converted.append(tuple(row.get(c) for c in columns))
        if converted:
            conn.exec_driver_sql(
                f'INSERT INTO "{replacement}" ({",".join(columns)}) VALUES ({",".join("?" for c in columns)})',
                converted,
            )
        # FK is suspended before BEGIN; original-first rename would redirect children.
        conn.exec_driver_sql(f'DROP TABLE "{name}"')
        conn.exec_driver_sql(f'ALTER TABLE "{replacement}" RENAME TO "{name}"')
        for sql in statements[1:]:
            conn.exec_driver_sql(sql)
    _validate_schema_manifest(conn, INPUT_MANIFEST, "historical platform input")


def _assert_legacy_source_preserved(conn: Connection, snapshot: dict) -> None:
    """Account for every original row/column, including removed actor host projections."""
    for name, old_rows in snapshot.items():
        new_rows = [
            dict(r)
            for r in conn.exec_driver_sql(
                f'SELECT * FROM "{name}" ORDER BY id'
            ).mappings()
        ]
        if len(old_rows) != len(new_rows):
            raise RuntimeError(f"Legacy row count changed: {name}")
        for old, new in zip(old_rows, new_rows, strict=True):
            for column, value in old.items():
                if name == "local_communities" and column in {
                    "discord_guild_id",
                    "discord_forum_channel_id",
                }:
                    continue
                if new.get(column) != value:
                    raise RuntimeError(
                        f"Legacy value changed: {name} row {old['id']} column {column}"
                    )
            if name == "local_communities":
                placement = conn.exec_driver_sql(
                    "SELECT external_channel_id,external_guild_id FROM platform_channel_surfaces WHERE local_community_id=?",
                    (old["id"],),
                ).one_or_none()
                if placement != (
                    str(old["discord_forum_channel_id"]),
                    str(old["discord_guild_id"]),
                ):
                    raise RuntimeError(
                        f"Legacy host location changed: community row {old['id']}"
                    )
    # Features absent from the release must not receive invented data.
    from .legacy_stage00_ddl import NORMALIZATION_DDL

    for name in set(NORMALIZATION_DDL) - set(snapshot):
        if conn.exec_driver_sql(f'SELECT count(*) FROM "{name}"').scalar_one():
            raise RuntimeError(f"Invented legacy feature rows: {name}")


def _upgrade_legacy_to_v1(
    conn: Connection, identity: IdentityCutoverConfig | None = None
) -> None:
    """Compose both historical phases without committing or changing the header."""
    # Validation, planning and normalization share the source protected by the
    # runner's writer lock; preflight evidence is never reused for writes.
    snapshot = _validate_legacy_release_v0(conn)
    expected, urls = plan_identity_cutover(snapshot, identity)
    _normalize_released_v0_to_platform_input(conn, snapshot)
    apply_identity_cutover(conn, snapshot, expected, urls)
    _upgrade_platform_model_from_legacy_input(conn)
    _assert_legacy_source_preserved(conn, expected)
    validate_current_identity(conn, identity)


DATABASE_VERSION = 1
MIGRATIONS: dict[int, Callable[[Connection, IdentityCutoverConfig | None], None]] = {
    1: _upgrade_legacy_to_v1
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
            _validate_legacy_release_v0(conn)
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


def _table_info(conn: Connection, table: str) -> list[object]:
    """Return raw SQLite table-info rows for one table."""
    return list(conn.execute(text(f"PRAGMA table_info({table})")).fetchall())


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
