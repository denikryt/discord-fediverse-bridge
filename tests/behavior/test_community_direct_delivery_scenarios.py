"""Remote Group direct-route behavior on the real User Actor delivery worker."""

from __future__ import annotations

from types import SimpleNamespace
from sqlalchemy import select
import pytest

from src.db import Database
from src.models import (
    BridgeActorFollow, UserActorOutboundDelivery, UserActorOutboundSource,
)
from src.user_actor_delivery import UserActorDeliveryService
from test_community_publish_durable_identity_scenarios import (
    GROUP, INBOX, setup_database, setup_action,
)


class Policy:
    """Exercise remote and Group authorization with a configurable deny list."""

    def __init__(self, blocked=()):
        """Keep one stable set of denied endpoints or actor IDs."""
        self.blocked = frozenset(blocked)

    def federation_decision(self, target):
        """Allow every target except those explicitly blocked by the test."""
        return SimpleNamespace(allowed=target not in self.blocked)


async def admitted_delivery(tmp_path):
    """Create an accepted Group Create in SQLite without sending it."""
    db = setup_database(tmp_path)
    service, renderer, _wake, thread, starter, _ = setup_action(db)
    result = await service.publish_thread_starter(thread=thread, starter_message=starter)
    assert result.status == "queued"
    renderer.send_user_actor_deliveries = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock()
    return db, renderer


@pytest.mark.asyncio
async def test_no_followers_direct_group_delivery_is_sent_once(tmp_path):
    """An accepted Group route is sufficient without fake User Actor followers."""
    db, gateway = await admitted_delivery(tmp_path)
    async def accept(**kwargs):
        """Acknowledge the single original Group target."""
        assert len(kwargs["deliveries"]) == 1
        assert kwargs["deliveries"][0]["targetEndpointUrl"] == INBOX
        return [SimpleNamespace(delivery_id=kwargs["deliveries"][0]["deliveryId"], ok=True, error=None)]
    gateway.send_user_actor_deliveries.side_effect = accept
    worker = UserActorDeliveryService(db, gateway, Policy())
    assert await worker.drain_deliveries() == 1
    assert await worker.drain_deliveries() == 0
    assert gateway.send_user_actor_deliveries.await_count == 1
    with db.session() as session:
        row = session.scalar(select(UserActorOutboundDelivery))
        assert row.status == "delivered" and row.attempt_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke,deny", [(True, None), (False, GROUP), (False, INBOX)])
async def test_revoked_or_policy_denied_group_blocks_every_send(tmp_path, revoke, deny):
    """No recipient can bypass the original accepted Group and inbox policy."""
    db, gateway = await admitted_delivery(tmp_path)
    if revoke:
        with db.immediate_session() as session:
            session.scalar(select(BridgeActorFollow)).cancel_requested = True
    worker = UserActorDeliveryService(db, gateway, Policy([deny] if deny else []))
    await worker.drain_deliveries()
    assert gateway.send_user_actor_deliveries.await_count == 0
    with db.session() as session:
        assert session.scalar(select(UserActorOutboundDelivery)).status == "ineligible"


@pytest.mark.asyncio
async def test_gateway_timeout_is_unresolved_and_cannot_be_blindly_resent(tmp_path):
    """A possibly completed external HTTP attempt remains unknown across worker scans."""
    db, gateway = await admitted_delivery(tmp_path)
    gateway.send_user_actor_deliveries.side_effect = TimeoutError("reply lost after send")
    worker = UserActorDeliveryService(db, gateway, Policy())
    assert await worker.drain_deliveries() == 1
    assert await worker.drain_deliveries() == 0
    assert gateway.send_user_actor_deliveries.await_count == 1
    with db.session() as session:
        delivery = session.scalar(select(UserActorOutboundDelivery))
        assert delivery.status == "unknown" and delivery.attempt_count == 1
        assert delivery.next_attempt_at is None


@pytest.mark.asyncio
async def test_stale_direct_group_sending_not_resubmitted_after_restart(tmp_path):
    """A crash after claiming a direct Group send leaves unknown evidence, not retry."""
    db, gateway = await admitted_delivery(tmp_path)
    from datetime import datetime, timedelta, timezone
    with db.immediate_session() as session:
        row = session.scalar(select(UserActorOutboundDelivery))
        row.status = "sending"
        row.claim_token = "previous-worker"
        row.claimed_at = datetime.now(timezone.utc) - timedelta(hours=1)
    worker = UserActorDeliveryService(db, gateway, Policy())
    assert await worker.drain_deliveries() == 0
    assert gateway.send_user_actor_deliveries.await_count == 0
    with db.session() as session:
        row = session.scalar(select(UserActorOutboundDelivery))
        assert row.status == "unknown"


@pytest.mark.asyncio
async def test_legacy_follower_only_still_uses_follower_grant(tmp_path):
    """New Group route admission must not reclassify unrelated follower-only sources."""
    # The existing personal-feed tests retain authoritative follower-only
    # coverage; this checkpoint explicitly records that it must stay separate.
    db = Database(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    db.create_all()
    assert not db.user_actor_deliveries.claim_due(limit=2, per_user_limit=2, stale_after_seconds=300)


@pytest.mark.asyncio
async def test_queued_root_and_reply_create_canonical_source_and_sibling_surfaces(tmp_path):
    """Locally committed queued root/reply must mirror before remote delivery."""
    from src.community_sync.runtime import CommunityRuntime
    from src.models import (
        ChannelCommunitySubscription, CommunityThreadGroup, CommunityMessageGroup,
        CommunityThreadGroupDelivery, CommunityMessageGroupDelivery,
    )
    db = setup_database(tmp_path)
    with db.immediate_session() as session:
        session.add(ChannelCommunitySubscription(
            discord_channel_id=101, discord_guild_id=222,
            lemmy_community_actor_id=GROUP, community_inbox_url=INBOX,
            follow_activity_id="https://bridge.example/actors/services/bridge/activities/follow/group42",
            status="accepted",
        ))
    service, gateway, wake, thread, starter, comment = setup_action(db)
    comment.channel = thread
    comment.reference = SimpleNamespace(message_id=starter.id)

    class Fanout:
        """Only the Discord edge is fake; SQLite canonical inserts remain real."""

        def __init__(self):
            """Record the actual sibling targets requested by runtime."""
            self.thread_count = 0
            self.comment_count = 0

        async def mirror_thread_to_siblings(self, *, source_thread, source_starter_message, sibling_channel_ids):
            """Return the one confirmed mirrored thread."""
            assert sibling_channel_ids == [101]
            self.thread_count += 1
            return [SimpleNamespace(channel_id=101, thread_id=991, starter_message_id=992)]

        async def mirror_message_to_siblings(self, *, source_message, sibling_thread_deliveries, reply_context):
            """Return the one confirmed mirrored reply with its parent reference."""
            assert len(sibling_thread_deliveries) == 1
            assert reply_context.get_reference_for_thread(991) == 992
            self.comment_count += 1
            return [SimpleNamespace(channel_id=101, thread_id=991, message_id=993)]

    fanout = Fanout()
    runtime = CommunityRuntime(database=db, content_publish_service=service, discord_fanout=fanout)
    root = await runtime.handle_discord_thread_create(thread, starter)
    assert root.status == "queued" and root.locally_accepted
    reply = await runtime.handle_discord_message(comment)
    assert reply.status == "queued" and reply.locally_accepted
    assert fanout.thread_count == 1 and fanout.comment_count == 1
    assert gateway.publish_content.await_count == 0
    with db.session() as session:
        assert len(list(session.scalars(select(CommunityThreadGroup)))) == 1
        assert len(list(session.scalars(select(CommunityMessageGroup)))) == 1
        assert len(list(session.scalars(select(CommunityThreadGroupDelivery)))) == 2
        assert len(list(session.scalars(select(CommunityMessageGroupDelivery)))) == 2
        assert len(list(session.scalars(select(UserActorOutboundSource)))) == 2
    # Duplicate events must not create an extra surface or second AP source.
    duplicate = await runtime.handle_discord_thread_create(thread, starter)
    assert duplicate.status == "ignored"
    assert fanout.thread_count == 1


@pytest.mark.asyncio
async def test_queued_rejection_does_not_create_discord_group(tmp_path):
    """A terminal precommit denial leaves no canonical Discord placement."""
    from src.community_sync.runtime import CommunityRuntime
    from src.models import CommunityThreadGroup
    db = setup_database(tmp_path, profile="unknown")
    service, gateway, wake, thread, starter, _comment = setup_action(db)
    runtime = CommunityRuntime(database=db, content_publish_service=service)
    result = await runtime.handle_discord_thread_create(thread, starter)
    assert result.status == "rejected"
    with db.session() as session:
        assert session.scalar(select(CommunityThreadGroup)) is None

@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_group", [False, True])
async def test_shared_endpoint_never_uses_follower_to_bypass_group_grant(
    tmp_path, revoke_group
):
    """A follower and Group with the same inbox share one target, not authority."""
    from src.models import (
        User, UserActorFollower, UserActorOutboundDeliveryFollower,
    )

    db = setup_database(tmp_path)
    with db.immediate_session() as session:
        author = session.scalar(select(User))
        session.add(UserActorFollower(
            user_id=author.id,
            remote_actor_id="https://other.example/actors/member",
            remote_inbox_url="https://other.example/inbox",
            remote_shared_inbox_url=INBOX,
            follow_activity_id="https://other.example/follow/1",
            status="accepted",
        ))
    service, gateway, _wake, thread, starter, _reply = setup_action(db)
    result = await service.publish_thread_starter(thread=thread, starter_message=starter)
    assert result.locally_accepted
    with db.session() as session:
        assert len(list(session.scalars(select(UserActorOutboundDelivery)))) == 1
        assert len(list(session.scalars(select(UserActorOutboundDeliveryFollower)))) == 1
    if revoke_group:
        with db.immediate_session() as session:
            session.scalar(select(BridgeActorFollow)).cancel_requested = True
    gateway.send_user_actor_deliveries = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(
        return_value=[SimpleNamespace(delivery_id=1, ok=True, error=None)]
    )
    worker = UserActorDeliveryService(db, gateway, Policy())
    await worker.drain_deliveries()
    assert gateway.send_user_actor_deliveries.await_count == (0 if revoke_group else 1)
    with db.session() as session:
        status = session.scalar(select(UserActorOutboundDelivery.status))
        assert status == ("ineligible" if revoke_group else "delivered")


@pytest.mark.asyncio
async def test_stale_direct_send_stays_unknown_after_group_revocation(tmp_path):
    """Revoking Follow after an unacknowledged send cannot erase send ambiguity."""
    from datetime import datetime, timedelta, timezone

    db, gateway = await admitted_delivery(tmp_path)
    with db.immediate_session() as session:
        row = session.scalar(select(UserActorOutboundDelivery))
        row.status = "sending"
        row.claim_token = "old-send"
        row.claimed_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.scalar(select(BridgeActorFollow)).cancel_requested = True
    worker = UserActorDeliveryService(db, gateway, Policy())
    await worker.drain_deliveries()
    assert gateway.send_user_actor_deliveries.await_count == 0
    with db.session() as session:
        assert session.scalar(select(UserActorOutboundDelivery)).status == "unknown"


def test_known_main_migration_preserves_existing_follower_delivery_fk_and_new_state(tmp_path):
    """A populated v0 delivery graph survives rebuilding the old SQLite CHECK."""
    import sqlite3
    from pathlib import Path
    from src.db.migrations import Stage4IdentityMigrationConfig

    source = Path(__file__).resolve().parent.parent / "fixtures/database/known_main_v0_schema.sql"
    db_file = tmp_path / "known-main-with-delivery.db"
    with sqlite3.connect(db_file) as conn:
        conn.executescript(source.read_text())
        conn.execute("""
            INSERT INTO users(id,discord_user_id,activitypub_username,actor_url,
                inbox_url,outbox_url,followers_url,public_key_pem,private_key_pem,
                profile_version,created_at,updated_at)
            VALUES(3,'123','alice','https://bridge.example/actors/users/alice',
                'https://bridge.example/actors/users/alice/inbox','https://bridge.example/actors/users/alice/outbox',
                'https://bridge.example/actors/users/alice/followers','public','private',1,'2026-01-01','2026-01-01')
        """)
        conn.execute("""
            INSERT INTO user_actor_outbound_sources(id,user_id,source_kind,
                ordering_key,logical_version,activity_id,snapshot_json,
                activity_json,state,render_attempt_count,created_at,updated_at)
            VALUES(6,3,'content_create','source/1',1,'https://bridge.test/create/1',
                '{}','{}','ready',0,'2026-01-01','2026-01-01')
        """)
        conn.execute("""
            INSERT INTO user_actor_outbound_deliveries(id,source_id,target_endpoint_url,
                status,attempt_count,created_at,updated_at)
            VALUES(9,6,'https://other.test/shared','delivered',2,'2026-01-01','2026-01-01')
        """)
        conn.execute("""
            INSERT INTO user_actor_outbound_delivery_followers(id,delivery_id,
                relationship_id_snapshot,remote_actor_id,snapshotted_endpoint_url)
            VALUES(11,9,27,'https://other.test/follower','https://other.test/shared')
        """)
    database = Database(f"sqlite:///{db_file}")
    identity = Stage4IdentityMigrationConfig("https://bridge.example", "bridge")
    database.migrate(identity)
    with sqlite3.connect(db_file) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT id,source_id,status,attempt_count "
                            "FROM user_actor_outbound_deliveries").fetchall() == [
            (9, 6, "delivered", 2),
        ]
        assert conn.execute("SELECT delivery_id,relationship_id_snapshot "
                            "FROM user_actor_outbound_delivery_followers").fetchall() == [(9, 27)]
        conn.execute("UPDATE user_actor_outbound_deliveries SET status='unknown' WHERE id=9")
    database.migrate(identity)
    with sqlite3.connect(db_file) as conn:
        assert conn.execute("SELECT status FROM user_actor_outbound_deliveries").fetchone() == ("unknown",)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    database.close()
