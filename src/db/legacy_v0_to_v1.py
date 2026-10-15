"""Direct released-v0 to ORM-v1 conversion; no intermediate business schema.

The caller owns the transaction, FK suspension, header and commit. Historical
SQL is loaded only in a disposable reference database. Live transfer uses native
SQL staging/copy; Python evidence is limited to identity/relationship metadata.
"""
from __future__ import annotations
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.resources import files
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection
from ..models import Base
from .legacy_identity_cutover import IDENTITY_FIELDS, plan_identity_cutover, apply_identity_cutover, validate_current_identity


@contextmanager
def release_reference():
    """Read the pinned SQL fact in a separate disposable database, never on the live connection."""
    engine = create_engine('sqlite://')
    try:
        with engine.connect() as reference:
            # executescript is safe here only: this engine contains no application data/transaction.
            reference.connection.driver_connection.executescript(files('src.db').joinpath('legacy_v0.sql').read_text())
            yield reference
    finally:
        engine.dispose()


def release_unique_columns():
    """Expose pinned uniqueness for identity collision planning without a second historical manifest."""
    from . import migrations
    with release_reference() as reference:
        return {name: migrations._table_manifest(reference, name)['unique'] for name in migrations._user_tables(reference)}


def capture_source_evidence(conn):
    """Read only identity and relationship metadata, omitting key material, bodies and timestamps."""
    result = {}
    unique = release_unique_columns()
    relationships = {'local_communities','local_subscribers','channel_community_subscriptions',
        'local_community_threads','local_community_messages','local_community_thread_surfaces',
        'local_community_message_surfaces','community_thread_groups','community_message_groups',
        'community_thread_group_deliveries','community_message_group_deliveries','discord_channel_snapshots'}
    for name in sorted(unique):
        fields = {r[1] for r in conn.exec_driver_sql(f'PRAGMA table_info("{name}")')}
        if not fields & IDENTITY_FIELDS and name not in relationships:
            # Untouched audit/policy/cache data stays in SQL staging, not duplicate Python rows.
            result[name] = []
            continue
        needed = {'id' ,'slug','activitypub_username','kind','object_kind','source_platform','status','channel_type','source_activity_json'} | IDENTITY_FIELDS
        needed |= {field for group in unique[name] for field in group}
        columns = sorted(field for field in fields if field in needed or field.endswith('_id'))
        result[name] = [dict(r) for r in conn.exec_driver_sql(f'SELECT {",".join(columns)} FROM main."{name}" ORDER BY id').mappings()]
    return result


def _validate_physical_identifiers(conn, names):
    """Validate SQLite values before numeric comparisons or CAST-based channel joins."""
    for name in names:
        info = conn.exec_driver_sql(f'PRAGMA main.table_info("{name}")').all()
        for row in info:
            field = row[1]
            physical = field.startswith('discord_') or field in {'source_channel_id','source_thread_id','source_message_id','source_starter_message_id','destination_channel_id','destination_thread_id','root_discord_channel_id','root_discord_message_id','thread_id','parent_discord_message_id'}
            if row[2] in {'INT','INTEGER'} and physical:
                # Affinity permits REAL/TEXT in integer columns; do not round or reinterpret snowflakes.
                bad = conn.exec_driver_sql(f'SELECT id FROM main."{name}" WHERE "{field}" IS NOT NULL AND (typeof("{field}") != \'integer\' OR "{field}" <= 0) LIMIT 1').first()
                if bad:
                    raise RuntimeError(f'Invalid legacy Discord location: {name} row {bad[0]} column {field}')


def validate_released_v0(conn):
    """Reject unknown schema/data before staging; return narrow immutable protocol evidence."""
    from . import migrations
    if conn.exec_driver_sql('PRAGMA user_version').scalar_one() != 0:
        raise RuntimeError('Released legacy source requires version 0')
    with release_reference() as reference:
        names = migrations._user_tables(reference)
        if migrations._user_tables(conn) != names or conn.exec_driver_sql("SELECT 1 FROM sqlite_master WHERE type='view'").first():
            raise RuntimeError('Unsupported released v0.1.0 schema inventory')
        for name in sorted(names):
            if migrations._table_manifest(conn,name) != migrations._table_manifest(reference,name):
                raise RuntimeError(f'Unsupported released v0.1.0 schema: {name}')
            for index in reference.exec_driver_sql(f'PRAGMA index_list("{name}")'):
                key = index[1]
                if conn.exec_driver_sql(f'PRAGMA index_xinfo("{key}")').all() != reference.exec_driver_sql(f'PRAGMA index_xinfo("{key}")').all():
                    raise RuntimeError(f'Unsupported released index semantics: {key}')
            # Exact normalized source DDL additionally covers deferrability, collation and hidden syntax.
            sql = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(name,)).scalar_one()
            expected = reference.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(name,)).scalar_one()
            # Constraint labels carry no semantics. Their definitions, collations
            # and FK options still participate in the exact normalized comparison.
            label = r"\bconstraint [a-z_][a-z_0-9]* (?=(?:check|unique|primary|foreign)\b)"
            actual_ddl = re.sub(label, '', migrations._normalized_sql(sql))
            expected_ddl = re.sub(label, '', migrations._normalized_sql(expected))
            if actual_ddl != expected_ddl:
                raise RuntimeError(f'Unsupported released v0.1.0 table definition: {name}')
    migrations._validate_sqlite_integrity(conn,'Released v0')
    _validate_physical_identifiers(conn,names)
    evidence = capture_source_evidence(conn)
    _validate_legacy_relationships(evidence)
    return evidence


def _historical_reply_indexes(snapshot: dict) -> tuple[dict, dict]:
    """Validate historical group/reply targets once, before any placement DDL.

    Group cycles and missing target-local parents cannot be repaired by a JOIN.
    Validate that historical graph before the physical replacement begins.
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


# Explicit source/target transfer rules; these are SQL statements, not another target DDL.
COPY_SQL = {
    'users': """
INSERT INTO main.users (
    id, discord_user_id, activitypub_username, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem, display_name, summary, avatar_media_id, header_media_id, profile_version, created_at, updated_at
)
SELECT
    l.id,
    l.discord_user_id,
    l.activitypub_username,
    l.actor_url,
    l.inbox_url,
    l.outbox_url,
    l.followers_url,
    l.public_key_pem,
    l.private_key_pem,
    NULL,
    NULL,
    NULL,
    NULL,
    0,
    l.created_at,
    l.updated_at
FROM temp.__legacy_users l

""",
    'message_mappings': """
INSERT INTO main.message_mappings (
    source_connector_id, source_channel_surface_id, id, source_platform, source_id, activity_id, object_id, actor_url, publication_context, community_actor_url, discord_guild_id, discord_channel_id, discord_message_id, created_at
)
SELECT
    CASE WHEN l.source_platform='discord' THEN 1 END,
    CASE WHEN l.source_platform='discord' THEN s.id END,
    l.id,
    l.source_platform,
    l.source_id,
    l.activity_id,
    l.object_id,
    l.actor_url,
    'community',
    l.community_actor_url,
    NULL,
    l.discord_channel_id,
    l.discord_message_id,
    l.created_at
FROM temp.__legacy_message_mappings l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.discord_channel_id AS TEXT)
""",
    'published_activity_objects': """
INSERT INTO main.published_activity_objects (
    source_connector_id, source_channel_surface_id, source_external_message_id, id, actor_username, actor_url, publication_context, community_actor_url, activity_id, object_id, kind, title, body_markdown, in_reply_to_object_id, conversation_root_object_id, discord_guild_id, discord_channel_id, discord_message_id, content_revision, deleted_at, desired_kind, desired_title, desired_body_markdown, desired_edited_timestamp, desired_fingerprint, discord_reconciliation_needed, discord_reconciliation_attempt_count, discord_reconciliation_next_attempt_at, discord_reconciliation_last_error, published_at, created_at
)
SELECT
    CASE WHEN l.discord_channel_id IS NOT NULL OR l.discord_message_id IS NOT NULL THEN 1 END,
    s.id,
    CAST(l.discord_message_id AS TEXT),
    l.id,
    l.actor_username,
    l.actor_url,
    'community',
    l.community_actor_url,
    l.activity_id,
    l.object_id,
    l.kind,
    l.title,
    l.body_markdown,
    l.in_reply_to_object_id,
    CASE WHEN l.kind='post' THEN l.object_id ELSE COALESCE(l.in_reply_to_object_id,l.object_id) END,
    NULL,
    l.discord_channel_id,
    l.discord_message_id,
    1,
    NULL,
    'none',
    NULL,
    NULL,
    NULL,
    NULL,
    0,
    0,
    NULL,
    NULL,
    l.published_at,
    l.created_at
FROM temp.__legacy_published_activity_objects l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.discord_channel_id AS TEXT)
""",
    'community_thread_groups': """
INSERT INTO main.community_thread_groups (
    source_connector_id, source_channel_surface_id, source_external_thread_id, id, community_actor_id, source_channel_id, source_thread_id, source_starter_message_id, ap_activity_id, ap_object_id, created_at
)
SELECT
    CASE WHEN s.id IS NOT NULL THEN 1 END,
    s.id,
    CAST(l.source_thread_id AS TEXT),
    l.id,
    l.community_actor_id,
    l.source_channel_id,
    l.source_thread_id,
    l.source_starter_message_id,
    l.ap_activity_id,
    l.ap_object_id,
    l.created_at
FROM temp.__legacy_community_thread_groups l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.source_channel_id AS TEXT)
""",
    'community_message_groups': """
INSERT INTO main.community_message_groups (
    source_connector_id, source_channel_surface_id, source_external_message_id, id, community_actor_id, thread_group_id, source_channel_id, source_thread_id, source_message_id, ap_activity_id, ap_object_id, parent_message_group_id, created_at
)
SELECT
    CASE WHEN s.id IS NOT NULL THEN 1 END,
    s.id,
    CAST(l.source_message_id AS TEXT),
    l.id,
    l.community_actor_id,
    l.thread_group_id,
    l.source_channel_id,
    l.source_thread_id,
    l.source_message_id,
    l.ap_activity_id,
    l.ap_object_id,
    l.parent_message_group_id,
    l.created_at
FROM temp.__legacy_community_message_groups l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.source_channel_id AS TEXT)
""",
    'local_communities': """
INSERT INTO main.local_communities (
    id, slug, display_name, summary, created_by_discord_user_id, actor_url, inbox_url, outbox_url, followers_url, public_key_pem, private_key_pem, status, created_at, updated_at
)
SELECT
    l.id,
    l.slug,
    l.display_name,
    l.summary,
    l.created_by_discord_user_id,
    l.actor_url,
    l.inbox_url,
    l.outbox_url,
    l.followers_url,
    l.public_key_pem,
    l.private_key_pem,
    l.status,
    l.created_at,
    l.updated_at
FROM temp.__legacy_local_communities l

""",
    'post_links': """
INSERT INTO main.post_links (
    connector_id, channel_surface_id, external_thread_id, external_starter_message_id, id, lemmy_post_id, lemmy_post_ap_id, discord_forum_channel_id, discord_forum_thread_id, discord_starter_message_id, direction, created_at
)
SELECT
    1,
    s.id,
    CAST(l.discord_forum_thread_id AS TEXT),
    CAST(l.discord_starter_message_id AS TEXT),
    l.id,
    l.lemmy_post_id,
    l.lemmy_post_ap_id,
    l.discord_forum_channel_id,
    l.discord_forum_thread_id,
    l.discord_starter_message_id,
    l.direction,
    l.created_at
FROM temp.__legacy_post_links l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.discord_forum_channel_id AS TEXT)
""",
    'comment_links': """
INSERT INTO main.comment_links (
    connector_id, channel_surface_id, external_thread_id, external_message_id, external_parent_message_id, id, lemmy_comment_id, lemmy_comment_ap_id, lemmy_parent_comment_ap_id, lemmy_post_id, discord_forum_thread_id, discord_message_id, direction, created_at
)
SELECT
    1,
    s.id,
    CAST(l.discord_forum_thread_id AS TEXT),
    CAST(l.discord_message_id AS TEXT),
    NULL,
    l.id,
    l.lemmy_comment_id,
    l.lemmy_comment_ap_id,
    l.lemmy_parent_comment_ap_id,
    l.lemmy_post_id,
    l.discord_forum_thread_id,
    l.discord_message_id,
    l.direction,
    l.created_at
FROM temp.__legacy_comment_links l
LEFT JOIN temp.__thread_locations t ON t.thread_id=l.discord_forum_thread_id
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(t.channel_id AS TEXT)
""",
    'community_thread_group_deliveries': """
INSERT INTO main.community_thread_group_deliveries (
    connector_id, channel_surface_id, external_thread_id, external_starter_message_id, id, thread_group_id, discord_channel_id, discord_thread_id, discord_starter_message_id, role
)
SELECT
    1,
    s.id,
    CAST(l.discord_thread_id AS TEXT),
    CAST(l.discord_starter_message_id AS TEXT),
    l.id,
    l.thread_group_id,
    l.discord_channel_id,
    l.discord_thread_id,
    l.discord_starter_message_id,
    l.role
FROM temp.__legacy_community_thread_group_deliveries l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.discord_channel_id AS TEXT)
""",
    'community_message_group_deliveries': """
INSERT INTO main.community_message_group_deliveries (
    connector_id, channel_surface_id, external_thread_id, external_message_id, external_parent_message_id, id, message_group_id, discord_channel_id, discord_thread_id, discord_message_id, role
)
SELECT
    1,
    s.id,
    CAST(l.discord_thread_id AS TEXT),
    CAST(l.discord_message_id AS TEXT),
    CAST(p.discord_message_id AS TEXT),
    l.id,
    l.message_group_id,
    l.discord_channel_id,
    l.discord_thread_id,
    l.discord_message_id,
    l.role
FROM temp.__legacy_community_message_group_deliveries l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.discord_channel_id AS TEXT)
JOIN temp.__legacy_community_message_groups g ON g.id=l.message_group_id
LEFT JOIN temp.__legacy_community_message_group_deliveries p ON p.message_group_id=g.parent_message_group_id AND p.discord_channel_id=l.discord_channel_id AND p.discord_thread_id=l.discord_thread_id
""",
    'local_community_thread_surfaces': """
INSERT INTO main.local_community_thread_surfaces (
    connector_id, channel_surface_id, external_thread_id, external_starter_message_id, id, local_community_thread_id, discord_forum_channel_id, discord_thread_id, discord_starter_message_id, local_subscriber_id, role, created_at
)
SELECT
    1,
    s.id,
    CAST(l.discord_thread_id AS TEXT),
    CAST(l.discord_starter_message_id AS TEXT),
    l.id,
    l.local_community_thread_id,
    l.discord_forum_channel_id,
    l.discord_thread_id,
    l.discord_starter_message_id,
    l.local_subscriber_id,
    l.role,
    l.created_at
FROM temp.__legacy_local_community_thread_surfaces l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.discord_forum_channel_id AS TEXT)
""",
    'local_community_message_surfaces': """
INSERT INTO main.local_community_message_surfaces (
    connector_id, channel_surface_id, external_message_id, external_parent_message_id, id, local_community_message_id, local_community_thread_surface_id, discord_forum_channel_id, discord_message_id, parent_discord_message_id, local_subscriber_id, role, created_at
)
SELECT
    1,
    s.id,
    CAST(l.discord_message_id AS TEXT),
    CAST(l.parent_discord_message_id AS TEXT),
    l.id,
    l.local_community_message_id,
    l.local_community_thread_surface_id,
    l.discord_forum_channel_id,
    l.discord_message_id,
    l.parent_discord_message_id,
    l.local_subscriber_id,
    l.role,
    l.created_at
FROM temp.__legacy_local_community_message_surfaces l
LEFT JOIN main.platform_channel_surfaces s ON s.connector_id=1 AND s.external_channel_id=CAST(l.discord_forum_channel_id AS TEXT)
""",
}


def _stage_source(conn, names):
    """Capture every source table in transactional TEMP before removing any main table."""
    for name in names:
        conn.exec_driver_sql(f'CREATE TEMP TABLE "__legacy_{name}" AS SELECT * FROM main."{name}"')
        if conn.exec_driver_sql(f'SELECT count(*) FROM temp."__legacy_{name}"').scalar_one() != conn.exec_driver_sql(f'SELECT count(*) FROM main."{name}"').scalar_one():
            raise RuntimeError(f'Incomplete source staging: {name}')
    for name in names:
        conn.exec_driver_sql(f'DROP TABLE main."{name}"')


def _create_channel_inventory(conn, names):
    """Merge persisted physical location evidence using SQLite, rejecting contradictory kinds/guilds."""
    fields = {'discord_channel_id','discord_forum_channel_id','source_channel_id','destination_channel_id','root_discord_channel_id'}
    forum_tables = {'local_communities','local_subscribers','channel_community_subscriptions','local_community_thread_surfaces','local_community_message_surfaces','community_thread_group_deliveries','community_message_group_deliveries','post_links'}
    queries = []
    for name in names:
        info = conn.exec_driver_sql(f'PRAGMA temp.table_info("__legacy_{name}")').all()
        columns = {r[1] for r in info}
        guild = 'discord_guild_id' if 'discord_guild_id' in columns else 'NULL'
        kind = "'forum'" if name in forum_tables else "'unknown'"
        if name == 'discord_channel_snapshots':
            kind = "CASE channel_type WHEN 'news' THEN 'announcement' WHEN 'announcement' THEN 'announcement' WHEN 'forum' THEN 'forum' WHEN 'text' THEN 'text' ELSE 'unknown' END"
        queries.extend(f'SELECT "{field}" channel, {guild} guild, {kind} kind FROM temp."__legacy_{name}" WHERE "{field}" IS NOT NULL' for field in sorted(fields & columns))
    evidence = ' UNION ALL '.join(queries)
    conn.exec_driver_sql('CREATE TEMP TABLE __channel_inventory AS WITH evidence AS (' + evidence + ") SELECT channel, MAX(guild) guild, COALESCE(MAX(NULLIF(kind,'unknown')),'unknown') kind, COUNT(DISTINCT guild) guild_count, COUNT(DISTINCT NULLIF(kind,'unknown')) kind_count FROM evidence GROUP BY channel")
    if conn.exec_driver_sql('SELECT channel FROM temp.__channel_inventory WHERE guild_count>1 OR kind_count>1 LIMIT 1').first():
        raise RuntimeError('Conflicting guilds/channel kinds in historical locations')
    conn.exec_driver_sql("INSERT INTO main.platform_connectors(connector_key,platform,instance_origin) VALUES('discord-primary','discord',NULL)")
    # Fresh target has exactly this one connector; stable numeric channel ordering preserves surface IDs.
    conn.exec_driver_sql("""
        INSERT INTO main.platform_channel_surfaces(connector_id,external_channel_id,external_guild_id,kind,created_at,updated_at)
        SELECT 1, CAST(channel AS TEXT), CAST(guild AS TEXT), kind, :now, :now
        FROM temp.__channel_inventory ORDER BY channel
    """, {'now': datetime.now(timezone.utc).isoformat()})
    conn.exec_driver_sql("""
        CREATE TEMP TABLE __thread_locations AS
        SELECT thread_id, MIN(channel_id) channel_id, COUNT(DISTINCT channel_id) count
        FROM (
            SELECT discord_thread_id thread_id, discord_forum_channel_id channel_id FROM temp.__legacy_local_community_thread_surfaces
            UNION ALL SELECT discord_thread_id, discord_channel_id FROM temp.__legacy_community_thread_group_deliveries
            UNION ALL SELECT discord_forum_thread_id, discord_forum_channel_id FROM temp.__legacy_post_links
        ) GROUP BY thread_id
    """)
    if conn.exec_driver_sql('SELECT thread_id FROM temp.__thread_locations WHERE count != 1 LIMIT 1').first():
        raise RuntimeError('Ambiguous/orphan legacy thread channel')


def _copy_source_rows(conn, names):
    """Transfer preserved source columns natively, using explicit SQL for changed families."""
    for name in names:
        if name in COPY_SQL:
            conn.exec_driver_sql(COPY_SQL[name])
        else:
            source = [r[1] for r in conn.exec_driver_sql(f'PRAGMA temp.table_info("__legacy_{name}")')]
            target = set(Base.metadata.tables[name].c.keys())
            if not set(source) <= target:
                raise RuntimeError(f'Unaccounted released columns: {name}')
            columns = ','.join('"'+column+'"' for column in source)
            conn.exec_driver_sql(f'INSERT INTO main."{name}" ({columns}) SELECT {columns} FROM temp."__legacy_{name}"')


def _claim_channels(conn):
    """Install typed domain ownership after owners and unclaimed physical channels exist."""
    for table, column, channel in [
        ('local_communities','local_community_id','discord_forum_channel_id'),
        ('local_subscribers','local_subscriber_id','discord_channel_id'),
        ('channel_community_subscriptions','remote_subscription_id','discord_channel_id'),
    ]:
        conn.exec_driver_sql(f'UPDATE main.platform_channel_surfaces SET "{column}"=(SELECT l.id FROM temp."__legacy_{table}" l WHERE CAST(l."{channel}" AS TEXT)=external_channel_id) WHERE connector_id=1 AND external_channel_id IN (SELECT CAST("{channel}" AS TEXT) FROM temp."__legacy_{table}")')


def _assert_preserved(conn, names, original, expected):
    """Apply only approved protocol deltas to staged evidence, then compare all original columns in SQL."""
    for name, rows in original.items():
        targets = {row['id']: row for row in expected[name]}
        for old in rows:
            new = targets.get(old['id'])
            if new is None:
                if name != 'remote_subscribers':
                    raise RuntimeError('Unapproved source deletion')
                conn.exec_driver_sql('DELETE FROM temp.__legacy_remote_subscribers WHERE id=?',(old['id'],))
                continue
            delta = {field:value for field,value in new.items() if value != old[field]}
            if delta:
                columns = ','.join('"'+field+'"=?' for field in delta)
                conn.exec_driver_sql(f'UPDATE temp."__legacy_{name}" SET {columns} WHERE id=?',(*delta.values(),old['id']))
    for name in names:
        fields = [r[1] for r in conn.exec_driver_sql(f'PRAGMA temp.table_info("__legacy_{name}")')]
        if name == 'local_communities':
            # Actor host projections moved to a typed physical claim, not a replacement actor field.
            fields = [field for field in fields if field not in {'discord_guild_id','discord_forum_channel_id'}]
            if conn.exec_driver_sql("""SELECT l.id FROM temp.__legacy_local_communities l LEFT JOIN main.platform_channel_surfaces s ON s.local_community_id=l.id
                WHERE s.external_channel_id IS NOT CAST(l.discord_forum_channel_id AS TEXT) OR s.external_guild_id IS NOT CAST(l.discord_guild_id AS TEXT) LIMIT 1""").first():
                raise RuntimeError('Legacy host location changed')
        columns = ','.join('"'+field+'"' for field in fields)
        source = f'SELECT {columns} FROM temp."__legacy_{name}"'
        target = f'SELECT {columns} FROM main."{name}"'
        for left,right in [(source,target),(target,source)]:
            if conn.exec_driver_sql(f'SELECT 1 FROM ({left} EXCEPT {right}) LIMIT 1').first():
                raise RuntimeError(f'Legacy value/row changed: {name}')
    for name in set(Base.metadata.tables) - set(names) - {'platform_connectors','platform_channel_surfaces'}:
        if conn.exec_driver_sql(f'SELECT count(*) FROM main."{name}"').scalar_one():
            raise RuntimeError(f'Invented legacy feature rows: {name}')


def upgrade_released_v0_to_v1(conn: Connection, identity=None):
    """Replace the released schema directly inside the runner's existing FK-suspended transaction."""
    from . import migrations, schema
    if conn.exec_driver_sql('PRAGMA foreign_keys').scalar_one() != 0:
        raise RuntimeError('Direct source replacement requires pre-transaction FK suspension')
    original = validate_released_v0(conn)
    expected, urls = plan_identity_cutover(original,identity)
    names = sorted(original)
    _stage_source(conn,names)
    schema.create_all(conn)
    _create_channel_inventory(conn,names)
    _copy_source_rows(conn,names)
    _claim_channels(conn)
    apply_identity_cutover(conn,original,expected,urls)
    migrations._install_current_schema_objects(conn)
    _assert_preserved(conn,names,original,expected)
    migrations._validate_current_v1_schema(conn)
    validate_current_identity(conn,identity)
    for name in names:
        conn.exec_driver_sql(f'DROP TABLE temp."__legacy_{name}"')
    conn.exec_driver_sql('DROP TABLE temp.__channel_inventory')
    conn.exec_driver_sql('DROP TABLE temp.__thread_locations')
