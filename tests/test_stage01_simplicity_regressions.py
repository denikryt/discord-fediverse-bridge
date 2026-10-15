"""Routing consistency and target-local reply identity during Stage 01 upgrade."""

import pytest
from sqlalchemy.orm import Session

from support.historical_platform_probe import upgrade_historical_probe
from src.db import Database
from src.db.repositories.platform_surfaces import PlatformSurfaceRepository
from src.models import PersonalFeedBinding
from src.personal_feeds import PersonalFeedService
from test_platform_data_model import _owners
from test_platform_data_model_migration_stage01 import _historical_stage00
import runpy
from pathlib import Path


@pytest.mark.parametrize(
    "projection", ["matching", "channel_mismatch", "guild_mismatch"]
)
def test_feed_service_uses_current_physical_claim(tmp_path, projection):
    """Account display and root admission agree with the durable claim repository."""
    db = Database(f"sqlite:///{tmp_path / 'feed.db'}")
    upgrade_historical_probe(db)
    try:
        with db.immediate_session() as session:
            owners = _owners(session)
            binding = session.get(
                PersonalFeedBinding, owners["personal_feed_binding_id"]
            )
            binding.discord_guild_id, binding.discord_channel_id = 1, 900
            surface = PlatformSurfaceRepository.ensure_channel(
                session,
                connector_key="discord-primary",
                external_channel_id="900",
                external_guild_id="1",
                kind="text",
            )
            PlatformSurfaceRepository.claim_personal_feed(
                session, surface.id, binding.id
            )
            # Historical projections may be inconsistent; only the physical claim admits routing.
            if projection == "channel_mismatch":
                binding.discord_channel_id = 901
            elif projection == "guild_mismatch":
                binding.discord_guild_id = 2
            guild, channel = binding.discord_guild_id, binding.discord_channel_id
        service = PersonalFeedService(db)
        expected = projection == "matching"
        assert (
            db.personal_feed_bindings.get_by_discord_user_id("1") is not None
        ) == expected
        assert (service.get_by_discord_user_id("1") is not None) == expected
        assert (service.resolve_root_binding(guild, channel) is not None) == expected
    finally:
        db.close()


@pytest.mark.parametrize(
    "scenario", ["matching", "missing_group", "missing_parent", "other_thread"]
)
def test_upgrade_resolves_only_target_local_parent(tmp_path, scenario):
    """Upgrade preserves reply identity or rolls back when its exact target is absent."""
    db = _historical_stage00(tmp_path / "legacy.db")
    old = runpy.run_path(str(Path(__file__).parent / "fixtures/platform_v1_models.py"))
    try:
        with Session(db.engine) as session:
            session.add(
                old["CommunityThreadGroup"](
                    id=1, community_actor_id="https://remote.test/c/news"
                )
            )
            session.add(
                old["CommunityMessageGroup"](
                    id=1,
                    thread_group_id=1,
                    community_actor_id="https://remote.test/c/news",
                )
            )
            session.add(
                old["CommunityMessageGroup"](
                    id=2,
                    thread_group_id=1,
                    community_actor_id="https://remote.test/c/news",
                    parent_message_group_id=1,
                )
            )
            if scenario != "missing_parent":
                session.add(
                    old["CommunityMessageGroupDelivery"](
                        id=1,
                        message_group_id=1,
                        discord_channel_id=900,
                        discord_thread_id=101 if scenario == "other_thread" else 100,
                        discord_message_id=111,
                        role="mirror",
                    )
                )
            session.add(
                old["CommunityMessageGroupDelivery"](
                    id=2,
                    message_group_id=99 if scenario == "missing_group" else 2,
                    discord_channel_id=900,
                    discord_thread_id=100,
                    discord_message_id=112,
                    role="mirror",
                )
            )
            session.commit()
        if scenario == "matching":
            upgrade_historical_probe(db)
            with db.engine.connect() as conn:
                assert (
                    conn.exec_driver_sql(
                        "SELECT external_parent_message_id FROM community_message_group_deliveries WHERE id=2"
                    ).scalar_one()
                    == "111"
                )
                assert conn.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        else:
            with pytest.raises(
                RuntimeError, match="Missing message group|Missing target-local parent"
            ):
                upgrade_historical_probe(db)
            with db.engine.connect() as conn:
                assert conn.exec_driver_sql("PRAGMA user_version").scalar_one() == 1
                assert (
                    conn.exec_driver_sql(
                        "SELECT discord_message_id FROM community_message_group_deliveries WHERE id=2"
                    ).scalar_one()
                    == 112
                )
    finally:
        db.close()
