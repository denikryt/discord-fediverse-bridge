"""Startup connector selection uses durable Stage 01 metadata, not a fallback."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from src.db import Database
from src.platform_types import ConnectorRef


def test_current_migrated_discord_connector_is_loaded_from_sqlite(tmp_path):
    """A migrated database authorizes exactly the configured Discord connector."""
    db = Database(f"sqlite:///{tmp_path / 'connector.db'}")
    try:
        db.migrate()
        assert db.platform_surfaces.get_discord_primary_connector() == ConnectorRef(
            'discord-primary', 'discord', None
        )
    finally:
        db.close()


@pytest.mark.parametrize('corruption', [
    'DELETE FROM platform_connectors WHERE connector_key = \'discord-primary\'',
    "UPDATE platform_connectors SET platform='fluxer', instance_origin='https://other.test' WHERE connector_key='discord-primary'",
])
def test_missing_or_wrong_platform_connector_blocks_startup(tmp_path, corruption):
    """A missing or incompatible persisted connector cannot start a Discord adapter."""
    db = Database(f"sqlite:///{tmp_path / 'connector.db'}")
    try:
        db.migrate()
        with db.engine.begin() as conn:
            conn.execute(text('PRAGMA foreign_keys=OFF'))  # New database has no channel claims.
            conn.execute(text(corruption))
        with pytest.raises(RuntimeError, match='discord-primary'):
            db.platform_surfaces.get_discord_primary_connector()
    finally:
        db.close()
