"""Direct released-source conversion, native staging and transactional rollback."""

import pytest
from sqlalchemy import event
from test_legacy_v0_to_v1_migration import legacy, _seed, _evidence


def test_startup_replaces_source_directly_without_stage00_tables(legacy):
    """One startup stages every source table before DROP and creates the ORM target once."""
    _seed(legacy)
    statements = []

    def trace(connection, cursor, statement, parameters, context, many):
        """Observe actual SQLite DDL rather than a mocked transfer callback."""
        statements.append(statement.strip())

    event.listen(legacy.engine, 'before_cursor_execute', trace)
    legacy.migrate()
    ddl = [s for s in statements if s.upper().startswith(('CREATE TABLE', 'CREATE TEMP TABLE', 'DROP TABLE'))]
    assert not any('_new_' in s for s in ddl)
    staging = [s for s in ddl if s.startswith('CREATE TEMP TABLE "__legacy_')]
    assert len(staging) == 30
    first_drop = next(i for i, s in enumerate(ddl) if s.upper().startswith('DROP TABLE'))
    assert len([s for s in ddl[:first_drop] if s.upper().startswith('CREATE TEMP TABLE')]) == 30
    with legacy.engine.connect() as c:
        assert c.exec_driver_sql('SELECT name FROM sqlite_temp_schema WHERE type="table"').all() == []
        assert c.exec_driver_sql('PRAGMA foreign_key_check').all() == []
        assert c.exec_driver_sql('PRAGMA integrity_check').scalar_one() == 'ok'
    before = _evidence(legacy)
    legacy.migrate()
    assert _evidence(legacy) == before


@pytest.mark.parametrize('phase', ['stage','drop','published','claim','identity','validation','stamp'])
def test_direct_fault_restores_source_and_cleans_temp(legacy, phase):
    """Failures across the physical replacement restore all v0 rows/DDL and SQLite enforcement."""
    from test_legacy_identity_cutover import _released_urls, _config

    _released_urls(legacy)
    before = _evidence(legacy)
    dropped = False

    def fail(connection, cursor, statement, parameters, context, many):
        """Inject on real SQL only after the writer enters the selected migration window."""
        nonlocal dropped
        sql = ' '.join(statement.lower().split())
        if sql.startswith('drop table main.'):
            dropped = True
        hit = (
            phase == 'stage' and sql.startswith('create temp table "__legacy_')
            or phase == 'drop' and sql.startswith('drop table main.')
            or phase == 'published' and sql.startswith('insert into main.published_activity_objects')
            or phase == 'claim' and sql.startswith('update main.platform_channel_surfaces')
            or phase == 'identity' and sql.startswith('update "users"')
            or phase == 'validation' and dropped and sql == 'pragma foreign_key_check'
            or phase == 'stamp' and sql.startswith('pragma user_version') and '=' in sql
        )
        if hit:
            raise RuntimeError('direct migration fault: ' + phase)

    event.listen(legacy.engine,'after_cursor_execute',fail)
    with pytest.raises(RuntimeError, match='direct migration fault'):
        legacy.migrate(identity=_config())
    event.remove(legacy.engine,'after_cursor_execute',fail)
    assert _evidence(legacy) == before
    with legacy.engine.connect() as c:
        assert c.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 1
        assert c.exec_driver_sql('SELECT name FROM sqlite_temp_schema WHERE type="table"').all() == []
    legacy.migrate(identity=_config())
    assert _evidence(legacy)[0] == 1


@pytest.mark.parametrize('table,column,value', [
    ('local_communities','discord_forum_channel_id','not-an-integer'),
    ('local_subscribers','discord_channel_id',1.5),
    ('local_communities','discord_guild_id','not-a-guild'),
])
def test_invalid_physical_affinity_is_rejected_before_staging(legacy, table, column, value):
    """Integer declarations cannot authorize rounding/text conversion of malformed snowflakes."""
    _seed(legacy)
    with legacy.engine.begin() as c:
        c.exec_driver_sql(f'UPDATE {table} SET {column}=?',(value,))
    before = _evidence(legacy)
    with pytest.raises(RuntimeError,match='Invalid legacy Discord location'):
        legacy.migrate()
    assert _evidence(legacy) == before


def test_reference_schema_matches_independent_release_and_evidence_omits_payloads(legacy):
    """Pinned SQL describes the tag; Python validation reads metadata rather than all source bodies/keys."""
    from src.db import migrations
    from src.db import legacy_v0_to_v1 as direct

    _seed(legacy)
    with legacy.engine.connect() as c, direct.release_reference() as reference:
        assert migrations._user_tables(c) == migrations._user_tables(reference)
        for name in migrations._user_tables(c):
            assert migrations._table_manifest(c,name) == migrations._table_manifest(reference,name)
        evidence = direct.capture_source_evidence(c)
        assert all('body_markdown' not in row for row in evidence['published_activity_objects'])
        assert all('private_key_pem' not in row for row in evidence['users'])
        assert all('private_key_data' not in row for row in evidence['bridge_actor_keys'])
        assert evidence['management_audit_events'] == []
