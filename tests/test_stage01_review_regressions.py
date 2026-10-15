"""Review regressions for claim admission and schema-only tooling contracts."""

import pytest

from src.db import Database
from src.db.repositories.platform_surfaces import PlatformSurfaceRepository
from src.models import ChannelSurface
from test_platform_data_model import _owners


@pytest.mark.parametrize("platform", ["discord", "fluxer"])
@pytest.mark.parametrize("kind", ["forum", "text", "announcement", "unknown"])
@pytest.mark.parametrize(
    "owner_column",
    [
        "local_community_id",
        "local_subscriber_id",
        "remote_subscription_id",
        "personal_feed_binding_id",
    ],
)
def test_claim_admits_only_owner_compatible_channel_kind(
    tmp_path, platform, kind, owner_column
):
    """A caller cannot reserve a channel that its owner's runtime cannot consume."""
    db = Database(f"sqlite:///{tmp_path / 'claims.db'}")
    db.migrate()
    try:
        with db.immediate_session() as session:
            owners = _owners(session)
            repo = PlatformSurfaceRepository
            if platform == "fluxer":
                repo.add_connector(
                    session,
                    connector_key="fluxer-a",
                    platform="fluxer",
                    instance_origin="https://fluxer.test",
                )
            surface = repo.ensure_channel(
                session,
                connector_key=(
                    "discord-primary" if platform == "discord" else "fluxer-a"
                ),
                external_channel_id="900",
                external_guild_id="1",
                kind=kind,
            )
            # Real Discord owners retain matching projections; Fluxer uses NULLs.
            if platform == "discord" and owner_column != "local_community_id":
                from src.db.repositories.platform_surfaces import OWNER_MODELS

                owner = session.get(OWNER_MODELS[owner_column], owners[owner_column])
                owner.discord_channel_id, owner.discord_guild_id = 900, 1
                session.flush()
            claim = getattr(
                repo,
                {
                    "local_community_id": "claim_local_host",
                    "local_subscriber_id": "claim_local_subscriber",
                    "remote_subscription_id": "claim_remote_subscription",
                    "personal_feed_binding_id": "claim_personal_feed",
                }[owner_column],
            )
            allowed = (
                kind in {"text", "announcement", "unknown"}
                if owner_column == "personal_feed_binding_id"
                else kind == "forum"
            )
            if allowed:
                claim(session, surface.id, owners[owner_column])
                assert getattr(surface, owner_column) == owners[owner_column]
                assert surface.role is not None
            else:
                with pytest.raises(ValueError, match="kind"):
                    claim(session, surface.id, owners[owner_column])
                session.refresh(surface)
                assert surface.role is None
                assert all(getattr(surface, column) is None for column in owners)
            surface_id = surface.id
        with db.session() as session:
            assert (session.get(ChannelSurface, surface_id).role is not None) == allowed
        with db.engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        db.close()


def test_create_all_is_schema_only_and_idempotent(tmp_path):
    """Explicit schema tooling never seeds connectors or stamps an app version."""
    db = Database(f"sqlite:///{tmp_path / 'schema.db'}")
    try:
        db.create_all()
        db.create_all()
        with db.engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA user_version").scalar_one() == 0
            assert (
                conn.exec_driver_sql(
                    "SELECT count(*) FROM platform_connectors"
                ).scalar_one()
                == 0
            )
            assert (
                next(
                    r
                    for r in conn.exec_driver_sql(
                        "PRAGMA table_xinfo(platform_channel_surfaces)"
                    )
                    if r[1] == "role"
                )[6]
                == 3
            )
        # A schema-only DB must not be silently adopted as a valid app database.
        with pytest.raises(RuntimeError):
            db.migrate()
    finally:
        db.close()


def test_migrate_bootstraps_version_and_connector_once(tmp_path):
    """Application startup explicitly initializes its complete versioned database."""
    db = Database(f"sqlite:///{tmp_path / 'app.db'}")
    try:
        db.migrate()
        db.migrate()
        with db.engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA user_version").scalar_one() == 1
            assert conn.exec_driver_sql(
                "SELECT connector_key FROM platform_connectors"
            ).scalars().all() == ["discord-primary"]
            assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    finally:
        db.close()
