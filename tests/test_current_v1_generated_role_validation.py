"""Current-schema admission rejects complete generated-role contract changes."""

import pytest

from src.db import Database
from test_legacy_v0_to_v1_migration import _evidence


@pytest.mark.parametrize("mutation", ["suffix", "wrapper", "virtual", "formatting"])
def test_current_startup_validates_whole_generated_role(tmp_path, mutation):
    """A syntactically valid replacement is rejected unchanged unless only formatting differs."""
    db = Database(f"sqlite:///{tmp_path / 'role.db'}")
    db.migrate()
    # Populate actual claims so the changed computation has an observable effect.
    with db.immediate_session() as session:
        repo = db.platform_surfaces
        from src.models import ChannelCommunitySubscription

        owner = ChannelCommunitySubscription(
            lemmy_community_actor_id="https://remote.test/c/news",
            status="accepted",
            discord_channel_id=900,
            discord_guild_id=1,
        )
        session.add(owner)
        session.flush()
        surface = repo.ensure_channel(
            session,
            connector_key="discord-primary",
            external_channel_id="900",
            external_guild_id="1",
            kind="forum",
        )
        repo.claim_remote_subscription(session, surface.id, owner.id)
    with db.engine.connect() as conn:
        ddl = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name='platform_channel_surfaces'"
        ).scalar_one()
        from src.models import ChannelSurface

        expression = str(ChannelSurface.__table__.c.role.computed.sqltext)
        assert expression in ddl
        if mutation == "suffix":
            ddl = ddl.replace(expression, expression + " || 'x'", 1)
        elif mutation == "wrapper":
            ddl = ddl.replace(
                expression, "COALESCE((" + expression + "), 'unclaimed')", 1
            )
        elif mutation == "virtual":
            ddl = ddl.replace(" STORED", " VIRTUAL", 1)
        else:
            ddl = ddl.replace("role VARCHAR(32)", '"role" VARCHAR(32)', 1).replace(
                expression, expression.replace("CASE WHEN", "case\nwhen"), 1
            )
        # Keep other definitions/rows intact. Replacement-before-drop avoids
        # redirecting foreign keys; enforcement is restored before startup.
        conn.rollback()
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.rollback()
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            conn.exec_driver_sql(
                ddl.replace(
                    "CREATE TABLE platform_channel_surfaces",
                    "CREATE TABLE _replacement_surfaces",
                    1,
                )
            )
            columns = [
                r[1]
                for r in conn.exec_driver_sql(
                    "PRAGMA table_xinfo(platform_channel_surfaces)"
                )
                if r[6] == 0
            ]
            names = ",".join(columns)
            conn.exec_driver_sql(
                f"INSERT INTO _replacement_surfaces ({names}) SELECT {names} FROM platform_channel_surfaces"
            )
            conn.exec_driver_sql("DROP TABLE platform_channel_surfaces")
            conn.exec_driver_sql(
                "ALTER TABLE _replacement_surfaces RENAME TO platform_channel_surfaces"
            )
            conn.commit()
        finally:
            conn.rollback()
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert conn.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        role = conn.exec_driver_sql(
            "SELECT role FROM platform_channel_surfaces"
        ).scalar_one()
        assert role == (
            "remote_subscriptionx" if mutation == "suffix" else "remote_subscription"
        )
    before = _evidence(db)
    try:
        if mutation == "formatting":
            db.migrate()
        else:
            with pytest.raises(RuntimeError, match="generated role|columns"):
                db.migrate()
        assert _evidence(db) == before
    finally:
        db.close()
