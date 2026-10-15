"""Operator-visible migration phases and safe failure context on real SQLite files."""

import logging
import sqlite3
import traceback

import pytest
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from src.db import Database
from test_legacy_v0_to_v1_migration import legacy, _seed, _evidence
from test_legacy_identity_cutover import _released_urls, _config

SECRET = "MIGRATION-PRIVATE-KEY-AND-BODY-MARKER"


def test_success_logs_phases_once_without_records_or_keys(legacy, caplog):
    """Real cutover reports phase order and commit; current startup does not replay work."""
    _released_urls(legacy)
    with legacy.engine.begin() as c:
        c.exec_driver_sql("UPDATE users SET private_key_pem=?", (SECRET,))
        c.exec_driver_sql(
            "UPDATE published_activity_objects SET body_markdown=?", (SECRET,)
        )
    caplog.set_level(logging.INFO, logger="src.db")
    legacy.migrate(identity=_config())
    messages = [r.getMessage() for r in caplog.records if r.name.startswith("src.db.")]
    expected = [
        "preflight",
        "started",
        "source schema",
        "transfer data",
        "ActivityPub identity cutover",
        "integrity",
        "committed",
    ]
    cursor = 0
    for fragment in expected:
        cursor = next(
            i + 1 for i in range(cursor, len(messages)) if fragment in messages[i]
        )
    assert SECRET not in caplog.text
    assert not any(r.exc_info for r in caplog.records)
    assert "rolled back" not in caplog.text
    caplog.clear()
    before = _evidence(legacy)
    legacy.migrate(identity=_config())
    assert _evidence(legacy) == before
    assert "transfer data" not in caplog.text
    assert "identity cutover" not in caplog.text
    assert "committed" not in caplog.text


@pytest.mark.parametrize(
    "phase,context",
    [
        ("stage", "staging source table"),
        ("drop", "dropping source table"),
        ("copy", "copy table published_activity_objects"),
        ("claim", "claim channels for local_communities"),
        ("identity", "identity cutover table users"),
        ("index", "install index published_activity_objects"),
        ("validation", "foreign_key_check"),
        ("stamp", "stamp version"),
        ("commit", "commit"),
    ],
)
def test_sql_failure_has_context_original_cause_and_safe_rollback(
    legacy, caplog, phase, context
):
    """A failure after actual SQL retains the cause and restores every original byte/value and FK mode."""
    _released_urls(legacy)
    before = _evidence(legacy)
    caplog.set_level(logging.INFO, logger="src.db")
    failures = []
    dropped = False

    def fail(connection, cursor, statement, parameters, execution_context, many):
        """Inject a driver-style SQLAlchemy failure only in the selected physical migration window."""
        nonlocal dropped
        sql = " ".join(statement.lower().split())
        if sql.startswith("drop table main."):
            dropped = True
        hit = (
            phase == "stage"
            and sql.startswith('create temp table "__legacy_')
            or phase == "drop"
            and sql.startswith("drop table main.")
            or phase == "copy"
            and sql.startswith("insert into main.published_activity_objects")
            or phase == "claim"
            and sql.startswith("update main.platform_channel_surfaces")
            or phase == "identity"
            and sql.startswith('update "users"')
            or phase == "index"
            and sql.startswith(
                "create index if not exists ix_published_objects_user_outbox"
            )
            or phase == "validation"
            and dropped
            and sql == "pragma foreign_key_check"
            or phase == "stamp"
            and sql.startswith("pragma user_version")
            and "=" in sql
        )
        if hit:
            error = OperationalError(
                statement,
                {"payload": SECRET},
                sqlite3.OperationalError("injected SQL fault"),
            )
            failures.append(error)
            raise error

    def fail_commit(connection):
        """Fail before physical COMMIT so the runner must restore the original source."""
        error = OperationalError(
            "COMMIT",
            {"payload": SECRET},
            sqlite3.OperationalError("injected SQL fault"),
        )
        failures.append(error)
        raise error

    kind, callback = (
        ("commit", fail_commit) if phase == "commit" else ("after_cursor_execute", fail)
    )
    event.listen(legacy.engine, kind, callback)
    try:
        with pytest.raises(RuntimeError, match=context) as caught:
            legacy.migrate(identity=_config())
    finally:
        event.remove(legacy.engine, kind, callback)
    causes = []
    cause = caught.value.__cause__
    while cause is not None:
        causes.append(cause)
        cause = cause.__cause__
    assert len(failures) == 1 and failures[0] in causes
    assert failures[0].hide_parameters
    assert SECRET not in "".join(traceback.format_exception(caught.value))
    assert SECRET not in caplog.text
    assert not any(r.exc_info for r in caplog.records)
    assert "rolled back" in caplog.text and "committed" not in caplog.text
    assert _evidence(legacy) == before
    with legacy.engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    legacy.migrate(identity=_config())


@pytest.mark.parametrize(
    "defect,fragments",
    [
        ("owner", ["channel_id=900"]),
        ("guild", ["channel_id=902"]),
        (
            "parent",
            ["local_subscribers.local_community_id", "child_id=9", "parent_id=777"],
        ),
        ("malformed_parent", ["child_id=9", "parent_id=<invalid str>"]),
        ("thread", ["thread_id=1000"]),
    ],
)
def test_conflict_error_identifies_only_relevant_ids(legacy, caplog, defect, fragments):
    """Conflicting scalar data is actionable without dumping the associated stored record."""
    _seed(legacy)
    with legacy.engine.begin() as c:
        if defect == "owner":
            c.exec_driver_sql(
                "UPDATE local_subscribers SET discord_channel_id=900 WHERE id=9"
            )
        elif defect == "guild":
            c.exec_driver_sql(
                "INSERT INTO discord_channel_snapshots(id,discord_channel_id,discord_guild_id,channel_name,channel_type,created_at,updated_at) VALUES(80,902,2,?,'forum',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
                (SECRET,),
            )
        elif defect == "parent":
            c.exec_driver_sql(
                "UPDATE local_subscribers SET local_community_id=777 WHERE id=9"
            )
        elif defect == "malformed_parent":
            c.exec_driver_sql(
                "UPDATE local_subscribers SET local_community_id=? WHERE id=9",
                (SECRET,),
            )
        else:
            c.exec_driver_sql(
                "UPDATE post_links SET discord_forum_thread_id=1000,discord_forum_channel_id=904 WHERE id=60"
            )
    before = _evidence(legacy)
    caplog.set_level(logging.INFO, logger="src.db")
    with pytest.raises(RuntimeError) as caught:
        legacy.migrate()
    for fragment in fragments:
        assert fragment in str(caught.value)
    assert SECRET not in str(caught.value) and SECRET not in caplog.text
    assert _evidence(legacy) == before


def test_fresh_bootstrap_logs_commit_without_cutover(tmp_path, caplog):
    """Fresh initialization logs actual completion without claiming historical identity work."""
    db = Database(f"sqlite:///{tmp_path/'fresh.db'}")
    caplog.set_level(logging.INFO, logger="src.db")
    db.migrate()
    assert "started" in caplog.text and "committed" in caplog.text
    assert "identity cutover" not in caplog.text and "rolled back" not in caplog.text
    db.close()


def test_reference_ddl_failure_retains_driver_cause_without_touching_source(
    legacy, monkeypatch, caplog
):
    """A broken packaged schema reports the direct DBAPI failure during read-only startup."""
    from types import SimpleNamespace
    from src.db import legacy_v0_to_v1 as direct

    _seed(legacy)
    before = _evidence(legacy)
    resource = SimpleNamespace(read_text=lambda: "CREATE TABLE broken (")
    monkeypatch.setattr(
        direct, "files", lambda package: SimpleNamespace(joinpath=lambda name: resource)
    )
    caplog.set_level(logging.INFO, logger="src.db")
    with pytest.raises(RuntimeError, match="released schema reference") as caught:
        legacy.migrate()
    assert isinstance(caught.value.__cause__, sqlite3.Error)
    assert _evidence(legacy) == before
    assert "committed" not in caplog.text and "rolled back" not in caplog.text
