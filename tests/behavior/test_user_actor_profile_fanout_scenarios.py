"""Behavior scenarios for durable User Actor profile update fanout."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.db import Database
from src.db.migrations import Stage4IdentityMigrationConfig
from src.models import (
    UserActorOutboundDelivery,
    UserActorOutboundDeliveryFollower,
    UserActorOutboundSource,
)
from src.profile_media import ProfileMediaStore
from src.user_actor_delivery import UserActorDeliveryService
from src.user_profiles import UserProfileService


class AllowAllPolicy:
    """Provide the non-secret current federation decision boundary."""

    def federation_decision(self, _endpoint: str) -> SimpleNamespace:
        """Allow every test endpoint."""
        return SimpleNamespace(allowed=True)


class FakeGateway:
    """Capture canonical render and per-target transport at the network edge."""

    def __init__(self) -> None:
        self.render_requests: list[dict] = []
        self.send_requests: list[dict] = []
        self.fail_targets: set[str] = set()

    async def render_user_actor_profile_update(
        self, *, activity_id: str, profile_snapshot: dict
    ) -> dict:
        """Return one complete stable public Update from immutable input."""
        self.render_requests.append(
            {"activity_id": activity_id, "snapshot": profile_snapshot}
        )
        return {
            "id": activity_id,
            "type": "Update",
            "actor": profile_snapshot["actorId"],
            "object": {
                "id": profile_snapshot["actorId"],
                "type": "Person",
                "name": profile_snapshot["displayName"],
            },
        }

    async def send_user_actor_deliveries(
        self, *, signing_actor_url: str, actor_username: str, deliveries: list[dict]
    ) -> list[SimpleNamespace]:
        """Return one isolated outcome for every submitted endpoint."""
        self.send_requests.append(
            {
                "signing_actor_url": signing_actor_url,
                "actor_username": actor_username,
                "deliveries": deliveries,
            }
        )
        return [
            SimpleNamespace(
                delivery_id=item["deliveryId"],
                ok=item["targetEndpointUrl"] not in self.fail_targets,
                error="remote unavailable"
                if item["targetEndpointUrl"] in self.fail_targets
                else None,
            )
            for item in deliveries
        ]


def _system(tmp_path):
    """Build the real Stage 4 persistence and service graph."""
    database = Database(f"sqlite:///{tmp_path / 'bridge.db'}")
    database.create_all()
    database.migrate()
    user = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    store = ProfileMediaStore(
        tmp_path / "media", max_upload_bytes=1024 * 1024, max_pixels=1_000_000
    )
    store.initialize()
    profiles = UserProfileService(database, store, "https://bridge.example")
    gateway = FakeGateway()
    deliveries = UserActorDeliveryService(database, gateway, AllowAllPolicy())
    return database, user, profiles, gateway, deliveries


def _follow(
    database: Database, user_id: int, actor: str, inbox: str, shared: str | None = None
) -> int:
    """Create one accepted relationship through the Stage 3 repository path."""
    with database.immediate_session() as session:
        transition = database.user_actor_followers.create_or_refresh(
            session,
            user_id=user_id,
            remote_actor_id=actor,
            remote_inbox_url=inbox,
            remote_shared_inbox_url=shared,
            follow_activity_id=f"{actor}/follows/1",
        )
        return transition.relationship_id


@pytest.mark.asyncio
async def test_profile_mutation_snapshots_shared_inbox_and_delivers_one_complete_update(
    tmp_path,
) -> None:
    """Two accepted actors sharing one endpoint produce one durable transport unit."""
    database, user, profiles, gateway, deliveries = _system(tmp_path)
    shared = "https://remote.example/inbox"
    first = _follow(
        database,
        user.id,
        "https://remote.example/users/a",
        "https://remote.example/users/a/inbox",
        shared,
    )
    second = _follow(
        database,
        user.id,
        "https://remote.example/users/b",
        "https://remote.example/users/b/inbox",
        shared,
    )

    result = profiles.update_identity_text(user.id, "Alice Example", "Bridge operator")
    summary = await deliveries.process_source(result.source_id)

    assert result.profile_version == 1
    assert result.audience_count == 1
    assert summary.total == 1 and summary.delivered == 1 and summary.outstanding == 0
    assert len(gateway.render_requests) == 1
    assert gateway.render_requests[0]["snapshot"]["displayName"] == "Alice Example"
    assert len(gateway.send_requests) == 1
    assert gateway.send_requests[0]["deliveries"][0]["targetEndpointUrl"] == shared
    with database.session() as session:
        source = session.get(UserActorOutboundSource, result.source_id)
        rows = list(
            session.scalars(
                select(UserActorOutboundDeliveryFollower)
                .join(UserActorOutboundDelivery)
                .where(UserActorOutboundDelivery.source_id == result.source_id)
                .order_by(UserActorOutboundDeliveryFollower.relationship_id_snapshot)
            )
        )
        assert source.state == "ready"
        assert "private" not in source.profile_snapshot_json.lower()
        assert "private" not in source.activity_json.lower()
        assert [row.relationship_id_snapshot for row in rows] == [first, second]


@pytest.mark.asyncio
async def test_follow_after_commit_is_not_added_to_historical_audience(
    tmp_path,
) -> None:
    """Audience ownership is fixed in the profile transaction, not at delivery time."""
    database, user, profiles, gateway, deliveries = _system(tmp_path)
    _follow(
        database, user.id, "https://one.example/users/a", "https://one.example/inbox"
    )
    result = profiles.replace_metadata(user.id, [("Site", "https://example.com")])
    _follow(
        database, user.id, "https://two.example/users/b", "https://two.example/inbox"
    )

    await deliveries.process_source(result.source_id)

    submitted = gateway.send_requests[0]["deliveries"]
    assert [item["targetEndpointUrl"] for item in submitted] == [
        "https://one.example/inbox"
    ]


@pytest.mark.asyncio
async def test_unfollow_and_refollow_does_not_reactivate_old_relationship_incarnation(
    tmp_path,
) -> None:
    """A new relationship ID cannot make historical failed work eligible again."""
    database, user, profiles, gateway, deliveries = _system(tmp_path)
    actor = "https://remote.example/users/a"
    endpoint = "https://remote.example/inbox"
    old_id = _follow(database, user.id, actor, endpoint)
    result = profiles.update_identity_text(user.id, "Old snapshot", None)
    gateway.fail_targets.add(endpoint)
    await deliveries.process_source(result.source_id)

    with database.immediate_session() as session:
        assert database.user_actor_followers.delete_relationship(
            session, user_id=user.id, remote_actor_id=actor
        )
    new_id = _follow(database, user.id, actor, endpoint)
    assert new_id != old_id
    gateway.fail_targets.clear()
    with database.session() as session:
        row = session.scalar(
            select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == result.source_id
            )
        )
        row.next_attempt_at = None

    await deliveries.drain_deliveries()

    with database.session() as session:
        row = session.scalar(
            select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == result.source_id
            )
        )
        assert row.status == "ineligible"
    assert len(gateway.send_requests) == 1


@pytest.mark.asyncio
async def test_failed_old_version_blocks_newer_version_for_same_endpoint(
    tmp_path,
) -> None:
    """A newer complete profile cannot bypass an older endpoint version in backoff."""
    database, user, profiles, gateway, deliveries = _system(tmp_path)
    endpoint = "https://remote.example/inbox"
    _follow(database, user.id, "https://remote.example/users/a", endpoint)
    first = profiles.update_identity_text(user.id, "Version one", None)
    second = profiles.update_identity_text(user.id, "Version two", None)
    gateway.fail_targets.add(endpoint)

    await deliveries.process_source(first.source_id)
    await deliveries.process_source(second.source_id)

    assert len(gateway.send_requests) == 1
    with database.session() as session:
        states = list(
            session.execute(
                select(
                    UserActorOutboundSource.logical_version,
                    UserActorOutboundDelivery.status,
                )
                .join(UserActorOutboundDelivery)
                .order_by(UserActorOutboundSource.logical_version)
            )
        )
        assert states == [(1, "failed"), (2, "pending")]


@pytest.mark.asyncio
async def test_partial_failure_retries_only_failed_endpoint_with_identical_activity(
    tmp_path,
) -> None:
    """Healthy targets stay terminal while retry reuses one persisted payload and ID."""
    database, user, profiles, gateway, deliveries = _system(tmp_path)
    first_endpoint = "https://one.example/inbox"
    second_endpoint = "https://two.example/inbox"
    _follow(database, user.id, "https://one.example/users/a", first_endpoint)
    _follow(database, user.id, "https://two.example/users/b", second_endpoint)
    gateway.fail_targets.add(second_endpoint)
    result = profiles.update_identity_text(user.id, "Stable profile", "Stable summary")

    first_summary = await deliveries.process_source(result.source_id)
    with database.session() as session:
        source = session.get(UserActorOutboundSource, result.source_id)
        first_activity_json = source.activity_json
        failed = session.scalar(
            select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == result.source_id,
                UserActorOutboundDelivery.target_endpoint_url == second_endpoint,
            )
        )
        failed.next_attempt_at = None

    gateway.fail_targets.clear()
    await deliveries.drain_deliveries()

    assert first_summary.delivered == 1 and first_summary.outstanding == 1
    assert len(gateway.render_requests) == 1
    assert len(gateway.send_requests) == 2
    assert [
        item["targetEndpointUrl"] for item in gateway.send_requests[1]["deliveries"]
    ] == [second_endpoint]
    assert (
        gateway.send_requests[0]["deliveries"][1]["activityJson"]
        == gateway.send_requests[1]["deliveries"][0]["activityJson"]
    )
    with database.session() as session:
        source = session.get(UserActorOutboundSource, result.source_id)
        rows = list(
            session.scalars(
                select(UserActorOutboundDelivery)
                .where(UserActorOutboundDelivery.source_id == result.source_id)
                .order_by(UserActorOutboundDelivery.target_endpoint_url)
            )
        )
        assert source.activity_json == first_activity_json
        assert [row.status for row in rows] == ["delivered", "delivered"]
        assert [row.attempt_count for row in rows] == [1, 2]


class DenyAllPolicy:
    """Reject every remote endpoint through the current global policy."""

    def federation_decision(self, _endpoint: str) -> SimpleNamespace:
        """Return one denied decision."""
        return SimpleNamespace(allowed=False)


@pytest.mark.asyncio
async def test_policy_denial_is_terminal_without_transport_attempt_or_secret_diagnostic(
    tmp_path,
) -> None:
    """Current policy suppresses retry before gateway submission and keeps bounded diagnostics."""
    database, user, profiles, gateway, _deliveries = _system(tmp_path)
    _follow(
        database,
        user.id,
        "https://remote.example/users/a",
        "https://remote.example/inbox",
    )
    result = profiles.update_identity_text(user.id, "Denied", None)
    service = UserActorDeliveryService(database, gateway, DenyAllPolicy())

    await service.process_source(result.source_id)

    assert gateway.send_requests == []
    with database.session() as session:
        row = session.scalar(
            select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == result.source_id
            )
        )
        assert row.status == "ineligible"
        assert row.attempt_count == 0
        assert row.claim_token is None
        assert "private" not in (row.last_error or "").lower()


@pytest.mark.asyncio
async def test_two_service_instances_submit_one_active_claim_for_same_source(
    tmp_path,
) -> None:
    """Independent runtime callers share the database claim as duplicate-send authority."""
    database, user, profiles, gateway, first_service = _system(tmp_path)
    _follow(
        database,
        user.id,
        "https://remote.example/users/a",
        "https://remote.example/inbox",
    )
    result = profiles.update_identity_text(user.id, "Concurrent", None)
    source = database.user_actor_deliveries.get_source(result.source_id)
    rendered = await gateway.render_user_actor_profile_update(
        activity_id=source.activity_id,
        profile_snapshot=__import__("json").loads(source.profile_snapshot_json),
    )
    database.user_actor_deliveries.record_rendered(result.source_id, rendered)
    second_service = UserActorDeliveryService(database, gateway, AllowAllPolicy())

    await __import__("asyncio").gather(
        first_service.drain_deliveries(), second_service.drain_deliveries()
    )

    assert sum(len(request["deliveries"]) for request in gateway.send_requests) == 1
    with database.session() as session:
        row = session.scalar(
            select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == result.source_id
            )
        )
        assert row.status == "delivered"
        assert row.attempt_count == 1


def test_legacy_stage3_database_migrates_profile_version_without_synthetic_source(
    tmp_path,
) -> None:
    """Existing users receive version zero while migration emits no historical update."""
    from sqlalchemy import create_engine, text

    database_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{database_path}", future=True)
    with engine.begin() as connection:
        connection.execute(
            text("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id VARCHAR(64) NOT NULL,
                activitypub_username VARCHAR(255) NOT NULL,
                actor_url VARCHAR(512) NOT NULL,
                inbox_url VARCHAR(512) NOT NULL,
                outbox_url VARCHAR(512) NOT NULL,
                followers_url VARCHAR(512) NOT NULL,
                public_key_pem TEXT NOT NULL,
                private_key_pem TEXT NOT NULL,
                display_name VARCHAR(30),
                summary VARCHAR(500),
                avatar_media_id INTEGER,
                header_media_id INTEGER
            )
        """)
        )
        connection.execute(
            text("""
                INSERT INTO users (
                    discord_user_id, activitypub_username, actor_url, inbox_url,
                    outbox_url, followers_url, public_key_pem, private_key_pem
                ) VALUES ('1', 'alice', 'https://bridge.example/actors/users/alice',
                    'https://bridge.example/actors/users/alice/inbox',
                    'https://bridge.example/actors/users/alice/outbox',
                    'https://bridge.example/actors/users/alice/followers', 'public', 'private')
            """)
        )
    engine.dispose()

    database = Database(f"sqlite:///{database_path}")
    database.create_all()
    database.migrate(
        Stage4IdentityMigrationConfig(
            public_origin="https://bridge.example", service_actor_identifier="bridge"
        )
    )

    with database.session() as session:
        assert (
            session.execute(text("SELECT profile_version FROM users")).scalar_one() == 0
        )
        assert (
            session.execute(
                text("SELECT COUNT(*) FROM user_actor_outbound_sources")
            ).scalar_one()
            == 0
        )


def test_source_state_constraints_reject_payload_state_mismatch(tmp_path) -> None:
    """SQLite prevents ready sources without payloads and pending sources with payloads."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    database, user, _profiles, _gateway, _deliveries = _system(tmp_path)
    base = {
        "user_id": user.id,
        "source_kind": "profile_update",
        "logical_version": 1,
        "activity_id": "https://bridge.example/actors/users/alice/activities/update/profile/invalid-1",
        "profile_snapshot_json": "{}",
    }
    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.execute(
                text("""
                    INSERT INTO user_actor_outbound_sources (
                        user_id, source_kind, logical_version, activity_id,
                        snapshot_json, activity_json, state,
                        render_attempt_count, created_at, updated_at
                    ) VALUES (
                        :user_id, :source_kind, :logical_version, :activity_id,
                        :profile_snapshot_json, NULL, 'ready', 0,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                """),
                base,
            )
    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.execute(
                text("""
                    INSERT INTO user_actor_outbound_sources (
                        user_id, source_kind, logical_version, activity_id,
                        snapshot_json, activity_json, state,
                        render_attempt_count, created_at, updated_at
                    ) VALUES (
                        :user_id, :source_kind, :logical_version, :activity_id,
                        :profile_snapshot_json, '{}', 'render_pending', 0,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                """),
                {
                    **base,
                    "logical_version": 2,
                    "activity_id": "https://bridge.example/actors/users/alice/activities/update/profile/invalid-2",
                },
            )


@pytest.mark.asyncio
async def test_fresh_service_recovers_committed_render_pending_source_without_new_mutation(
    tmp_path,
) -> None:
    """A restart renders and delivers the exact committed snapshot without user action."""
    database, user, profiles, _gateway, _deliveries = _system(tmp_path)
    endpoint = "https://remote.example/inbox"
    _follow(database, user.id, "https://remote.example/users/a", endpoint)
    result = profiles.update_identity_text(user.id, "Committed before crash", None)

    recovered_gateway = FakeGateway()
    recovered_service = UserActorDeliveryService(
        database, recovered_gateway, AllowAllPolicy()
    )
    await recovered_service.drain_once()

    assert (
        recovered_gateway.render_requests[0]["snapshot"]["displayName"]
        == "Committed before crash"
    )
    assert (
        recovered_gateway.send_requests[0]["deliveries"][0]["targetEndpointUrl"]
        == endpoint
    )
    with database.session() as session:
        source = session.get(UserActorOutboundSource, result.source_id)
        delivery = session.scalar(
            select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == result.source_id
            )
        )
        assert source.state == "ready"
        assert delivery.status == "delivered"


@pytest.mark.asyncio
async def test_stale_sending_claim_is_reclaimed_after_restart_but_fresh_claim_is_not(
    tmp_path,
) -> None:
    """Crash recovery uses the same ordering claim and never steals a live owner."""
    from datetime import datetime, timedelta, timezone

    database, user, profiles, gateway, service = _system(tmp_path)
    _follow(
        database,
        user.id,
        "https://remote.example/users/a",
        "https://remote.example/inbox",
    )
    result = profiles.update_identity_text(user.id, "Claim recovery", None)
    source = database.user_actor_deliveries.get_source(result.source_id)
    activity = await gateway.render_user_actor_profile_update(
        activity_id=source.activity_id,
        profile_snapshot=__import__("json").loads(source.profile_snapshot_json),
    )
    database.user_actor_deliveries.record_rendered(result.source_id, activity)
    first_claim = database.user_actor_deliveries.claim_due(
        limit=1, per_user_limit=1, stale_after_seconds=300
    )
    assert len(first_claim) == 1
    assert (
        database.user_actor_deliveries.claim_due(
            limit=1, per_user_limit=1, stale_after_seconds=300
        )
        == []
    )
    with database.session() as session:
        row = session.get(UserActorOutboundDelivery, first_claim[0].delivery_id)
        row.claimed_at = datetime.now(timezone.utc) - timedelta(seconds=301)

    await service.drain_deliveries()

    with database.session() as session:
        row = session.get(UserActorOutboundDelivery, first_claim[0].delivery_id)
        assert row.status == "delivered"
        assert row.attempt_count == 1
    assert sum(len(request["deliveries"]) for request in gateway.send_requests) == 1


def test_claim_batch_applies_global_and_per_user_fairness_caps(tmp_path) -> None:
    """One large follower set cannot consume every claim in a bounded pass."""
    database, first_user, profiles, gateway, _service = _system(tmp_path)
    second_user = database.users.create_user(
        discord_user_id="2",
        activitypub_username="bob",
        actor_url="https://bridge.example/actors/users/bob",
        inbox_url="https://bridge.example/actors/users/bob/inbox",
        outbox_url="https://bridge.example/actors/users/bob/outbox",
        followers_url="https://bridge.example/actors/users/bob/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    for index in range(3):
        _follow(
            database,
            first_user.id,
            f"https://first-{index}.example/users/a",
            f"https://first-{index}.example/inbox",
        )
        _follow(
            database,
            second_user.id,
            f"https://second-{index}.example/users/a",
            f"https://second-{index}.example/inbox",
        )
    first = profiles.update_identity_text(first_user.id, "Alice", None)
    second = profiles.update_identity_text(second_user.id, "Bob", None)
    for result in (first, second):
        source = database.user_actor_deliveries.get_source(result.source_id)
        activity = {
            "id": source.activity_id,
            "type": "Update",
            "actor": __import__("json").loads(source.profile_snapshot_json)["actorId"],
        }
        database.user_actor_deliveries.record_rendered(result.source_id, activity)

    claims = database.user_actor_deliveries.claim_due(
        limit=4, per_user_limit=1, stale_after_seconds=300
    )

    assert len(claims) == 2
    assert {claim.user_id for claim in claims} == {first_user.id, second_user.id}


class SelectivePolicyFailure:
    """Raise for one endpoint while allowing unrelated transport work."""

    def federation_decision(self, value: str) -> SimpleNamespace:
        """Model one policy persistence read failure."""
        if "one.example" in value:
            raise RuntimeError("policy unavailable")
        return SimpleNamespace(allowed=True)


@pytest.mark.asyncio
async def test_policy_read_failure_releases_only_affected_claim(tmp_path) -> None:
    """One policy failure remains retryable and does not block another endpoint."""
    database, user, profiles, gateway, _service = _system(tmp_path)
    _follow(
        database, user.id, "https://one.example/users/a", "https://one.example/inbox"
    )
    _follow(
        database, user.id, "https://two.example/users/b", "https://two.example/inbox"
    )
    result = profiles.update_identity_text(user.id, "Policy isolation", None)
    service = UserActorDeliveryService(database, gateway, SelectivePolicyFailure())

    await service.process_source(result.source_id)

    assert [
        item["targetEndpointUrl"]
        for request in gateway.send_requests
        for item in request["deliveries"]
    ] == ["https://two.example/inbox"]
    with database.session() as session:
        rows = list(
            session.scalars(
                select(UserActorOutboundDelivery)
                .where(UserActorOutboundDelivery.source_id == result.source_id)
                .order_by(UserActorOutboundDelivery.target_endpoint_url)
            )
        )
        assert [(row.status, row.attempt_count) for row in rows] == [
            ("failed", 0),
            ("delivered", 1),
        ]


def test_claim_due_supersedes_older_work_after_newer_delivery(tmp_path) -> None:
    """Claim evaluation terminally skips an old version already replaced remotely."""
    database, user, profiles, _gateway, _service = _system(tmp_path)
    endpoint = "https://remote.example/inbox"
    _follow(database, user.id, "https://remote.example/users/a", endpoint)
    older = profiles.update_identity_text(user.id, "Older", None)
    newer = profiles.update_identity_text(user.id, "Newer", None)

    for result in (older, newer):
        source = database.user_actor_deliveries.get_source(result.source_id)
        database.user_actor_deliveries.record_rendered(
            result.source_id,
            {"id": source.activity_id, "type": "Update", "actor": user.actor_url},
        )

    with database.session() as session:
        newer_delivery = session.scalar(
            select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == newer.source_id
            )
        )
        newer_delivery.status = "delivered"

    claims = database.user_actor_deliveries.claim_due(
        limit=10, per_user_limit=10, stale_after_seconds=300
    )

    assert claims == []
    with database.session() as session:
        older_delivery = session.scalar(
            select(UserActorOutboundDelivery).where(
                UserActorOutboundDelivery.source_id == older.source_id
            )
        )
        assert older_delivery.status == "superseded"
        assert older_delivery.claim_token is None
        assert older_delivery.claimed_at is None
