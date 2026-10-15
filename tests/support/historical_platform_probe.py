"""Isolated historical-shape proof; never an application startup/migration route."""

from src.db import migrations


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
            migrations._upgrade_platform_model_from_legacy_input(conn)
            conn.exec_driver_sql("PRAGMA user_version=1")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            migrations._restore_foreign_keys(conn)
    database.migrate()
