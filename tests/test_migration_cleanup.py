"""Historical non-FK relationships must fail before destructive SQL staging."""

import pytest
from sqlalchemy import event
from test_legacy_v0_to_v1_migration import legacy, _seed, _evidence


@pytest.mark.parametrize(
    "table,column,row",
    [
        ("local_subscribers", "local_community_id", 9),
        ("remote_subscribers", "local_community_id", 30),
        ("local_community_threads", "local_community_id", 50),
        ("local_community_messages", "local_community_thread_id", 51),
        ("local_community_thread_surfaces", "local_community_thread_id", 52),
        ("local_community_thread_surfaces", "local_subscriber_id", 52),
        ("local_community_message_surfaces", "local_community_message_id", 53),
        ("local_community_message_surfaces", "local_community_thread_surface_id", 53),
        ("community_message_groups", "thread_group_id", 42),
        ("community_message_groups", "parent_message_group_id", 43),
        ("community_thread_group_deliveries", "thread_group_id", 41),
        ("community_message_group_deliveries", "message_group_id", 44),
    ],
)
def test_missing_historical_parent_is_rejected_before_staging(
    legacy, table, column, row
):
    """SQLite accepts a released scalar orphan; startup rejects it without changing any evidence."""
    _seed(legacy)
    with legacy.engine.begin() as conn:
        conn.exec_driver_sql(f'UPDATE "{table}" SET "{column}"=777 WHERE id=?', (row,))
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    before = _evidence(legacy)
    statements = []

    def trace(connection, cursor, statement, parameters, context, many):
        """Observe real statements to prove validation precedes destructive staging."""
        statements.append(statement.lower())

    event.listen(legacy.engine, "before_cursor_execute", trace)
    try:
        with pytest.raises(
            RuntimeError, match="Missing legacy parent|Missing message group"
        ):
            legacy.migrate()
    finally:
        event.remove(legacy.engine, "before_cursor_execute", trace)
    assert not any(
        s.startswith("create temp table") or s.startswith("drop table")
        for s in statements
    )
    assert _evidence(legacy) == before


@pytest.mark.parametrize("change", ["extra_index", "index_collation"])
def test_current_startup_rejects_changed_native_index_contract(tmp_path, change):
    """A complete v1 with an extra index or changed collation is rejected without repair."""
    from src.db import Database

    db = Database(f"sqlite:///{tmp_path/'indexes.db'}")
    db.migrate()
    with db.engine.begin() as c:
        if change == "extra_index":
            c.exec_driver_sql("CREATE INDEX rogue_users ON users(activitypub_username)")
        else:
            c.exec_driver_sql("DROP INDEX ix_published_objects_user_outbox")
            c.exec_driver_sql(
                "CREATE INDEX ix_published_objects_user_outbox ON published_activity_objects(actor_username COLLATE NOCASE,actor_url,deleted_at,published_at,id)"
            )
    before = _evidence(db)
    with pytest.raises(RuntimeError, match="current v1 schema"):
        db.migrate()
    assert _evidence(db) == before
    db.close()


def test_native_schema_comparison_preserves_constraint_words_inside_literals():
    """Two valid CHECKs with different forbidden values must remain different schema evidence."""
    from sqlalchemy import create_engine
    from src.db.migrations import _table_manifest

    engine = create_engine("sqlite://")
    try:
        with engine.connect() as c:
            c.exec_driver_sql(
                "CREATE TABLE terms(id INTEGER PRIMARY KEY, label TEXT CHECK(label != 'constraint marker check'))"
            )
            first = _table_manifest(c, "terms")
            c.exec_driver_sql("INSERT INTO terms VALUES (1, 'check')")
            c.exec_driver_sql("DROP TABLE terms")
            c.exec_driver_sql(
                "CREATE TABLE terms(id INTEGER PRIMARY KEY, label TEXT CHECK(label != 'check'))"
            )
            second = _table_manifest(c, "terms")
            assert first != second
    finally:
        engine.dispose()


@pytest.mark.parametrize("cycle", ["self", "pair"])
def test_cyclic_historical_replies_are_rejected_without_changes(legacy, cycle):
    """Recursive ancestry must terminate and reject both self and multi-row cycles."""
    _seed(legacy)
    with legacy.engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE community_message_groups SET parent_message_group_id=? WHERE id=42",
            (42 if cycle == "self" else 43,),
        )
    before = _evidence(legacy)
    with pytest.raises(RuntimeError, match="Cyclic legacy parent"):
        legacy.migrate()
    assert _evidence(legacy) == before
