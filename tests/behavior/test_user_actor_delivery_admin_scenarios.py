"""Behavior tests for safe operator delivery administration."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from src.db import Database
from src.models import (
    UserActorFollower,
    UserActorOutboundDelivery,
    UserActorOutboundDeliveryFollower,
    UserActorOutboundSource,
)
from src.user_actor_delivery_admin import main


def _database(tmp_path):
    """Create one real database with two users and one immutable delivery graph."""
    path = tmp_path / "admin.db"
    database = Database(f"sqlite:///{path}")
    database.create_all()
    database.migrate()
    alice = database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.test/actors/users/alice",
        inbox_url="https://bridge.test/actors/users/alice/inbox",
        outbox_url="https://bridge.test/actors/users/alice/outbox",
        followers_url="https://bridge.test/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    database.users.create_user(
        discord_user_id="2",
        activitypub_username="bob",
        actor_url="https://bridge.test/actors/bob",
        inbox_url="https://bridge.test/actors/bob/inbox",
        outbox_url="https://bridge.test/actors/bob/outbox",
        followers_url="https://bridge.test/actors/bob/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    with database.session() as session:
        source = UserActorOutboundSource(
            user_id=alice.id,
            source_kind="content_create",
            ordering_key="object:1",
            logical_version=1,
            activity_id="https://bridge.test/activities/1",
            snapshot_json="{}",
            activity_json="{}",
            state="ready",
        )
        session.add(source)
        session.flush()
        follower = UserActorFollower(
            user_id=alice.id,
            remote_actor_id="https://remote.test/users/follower",
            remote_inbox_url="https://remote.test/inbox",
            remote_shared_inbox_url=None,
            follow_activity_id="https://remote.test/follows/1",
            status="accepted",
        )
        session.add(follower)
        session.flush()
        failed = UserActorOutboundDelivery(
            source_id=source.id,
            target_endpoint_url="https://remote.test/inbox",
            status="failed",
            attempt_count=2,
            last_attempted_at=datetime(2026, 6, 18, tzinfo=timezone.utc),
            last_error=(
                "-----BEGIN PRIVATE KEY-----abc-----END PRIVATE KEY----- "
                "Bearer secret-token connection failed"
            ),
        )
        delivered = UserActorOutboundDelivery(
            source_id=source.id,
            target_endpoint_url="https://other.test/inbox",
            status="delivered",
        )
        session.add_all([failed, delivered])
        session.flush()
        session.add(
            UserActorOutboundDeliveryFollower(
                delivery_id=failed.id,
                relationship_id_snapshot=follower.id,
                remote_actor_id="https://remote.test/users/follower",
                snapshotted_endpoint_url=failed.target_endpoint_url,
            )
        )
        return database, path, failed.id, delivered.id, source.id


def test_retry_requires_exact_or_explicit_bounded_bulk_selector(tmp_path) -> None:
    """An operator cannot accidentally reset every failed delivery."""
    database_url = f"sqlite:///{tmp_path / 'admin.db'}"
    with pytest.raises(SystemExit):
        main(["--database-url", database_url, "retry"])
    with pytest.raises(SystemExit):
        main(["--database-url", database_url, "retry", "--username", "alice"])
    with pytest.raises(SystemExit):
        main(
            [
                "--database-url",
                database_url,
                "retry",
                "--username",
                "alice",
                "--all-failed",
            ]
        )


def test_list_exposes_bounded_operator_diagnostics_and_redacts_errors(
    tmp_path, capsys
) -> None:
    """Local diagnostics identify the activity, endpoint, audience, and attempts."""
    database, path, failed_id, _delivered_id, source_id = _database(tmp_path)
    database.close()
    assert (
        main(["--database-url", f"sqlite:///{path}", "list", "--status", "failed"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {
            "activity_id": "https://bridge.test/activities/1",
            "attempt_count": 2,
            "delivery_id": failed_id,
            "last_attempted_at": "2026-06-18 00:00:00",
            "last_error": "[redacted private key] Bearer [redacted] connection failed",
            "logical_version": 1,
            "next_attempt_at": None,
            "represented_actor_ids": ["https://remote.test/users/follower"],
            "source_id": source_id,
            "source_kind": "content_create",
            "status": "failed",
            "target_endpoint_url": "https://remote.test/inbox",
            "username": "alice",
        }
    ]


def test_exact_retry_reports_not_found_ownership_and_non_retryable_statuses(
    tmp_path, capsys
) -> None:
    """Automation receives a nonzero status whenever no failed row was reset."""
    database, path, failed_id, delivered_id, _source_id = _database(tmp_path)
    database.close()
    url = f"sqlite:///{path}"
    assert main(["--database-url", url, "retry", "--delivery-id", "9999"]) == 3
    assert (
        main(
            [
                "--database-url",
                url,
                "retry",
                "--delivery-id",
                str(failed_id),
                "--username",
                "bob",
            ]
        )
        == 4
    )
    assert (
        main(["--database-url", url, "retry", "--delivery-id", str(delivered_id)]) == 5
    )
    assert main(["--database-url", url, "retry", "--delivery-id", str(failed_id)]) == 0
    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [item["outcome"] for item in outputs] == [
        "not_found",
        "ownership_mismatch",
        "not_retryable",
        "success",
    ]


def test_bulk_retry_is_bounded_and_reports_remaining_not_skipped(
    tmp_path, capsys
) -> None:
    """Rows outside the deterministic batch remain queued for a later invocation."""
    database, path, _failed_id, _delivered_id, source_id = _database(tmp_path)
    with database.session() as session:
        for index in range(2):
            session.add(
                UserActorOutboundDelivery(
                    source_id=source_id,
                    target_endpoint_url=f"https://bulk{index}.test/inbox",
                    status="failed",
                )
            )
    database.close()
    assert (
        main(
            [
                "--database-url",
                f"sqlite:///{path}",
                "retry",
                "--username",
                "alice",
                "--all-failed",
                "--limit",
                "2",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "matched": 3,
        "outcome": "success",
        "remaining": 1,
        "reset": 2,
        "skipped": 0,
    }


def test_worker_claim_and_operator_retry_are_serialized_by_compare_and_set(
    tmp_path,
) -> None:
    """A racing worker claim cannot be reverted from sending back to pending."""
    database, _path, failed_id, _delivered_id, _source_id = _database(tmp_path)
    barrier = threading.Barrier(3)
    retry_results = []
    claims = []

    def retry() -> None:
        barrier.wait()
        retry_results.append(
            database.user_actor_deliveries.retry_failed_by_delivery_id(failed_id)
        )

    def claim() -> None:
        barrier.wait()
        claims.extend(
            database.user_actor_deliveries.claim_due(
                limit=10, per_user_limit=10, stale_after_seconds=300
            )
        )

    retry_thread = threading.Thread(target=retry)
    claim_thread = threading.Thread(target=claim)
    retry_thread.start()
    claim_thread.start()
    barrier.wait()
    retry_thread.join(timeout=5)
    claim_thread.join(timeout=5)
    assert not retry_thread.is_alive() and not claim_thread.is_alive()
    assert retry_results[0].outcome in {"success", "not_retryable"}
    with database.session() as session:
        delivery = session.get(UserActorOutboundDelivery, failed_id)
        assert delivery is not None
        assert delivery.status == "sending"
        assert delivery.claim_token is not None
    assert len(claims) == 1


def test_source_summary_counts_worker_outstanding_statuses_through_facade(
    tmp_path,
) -> None:
    """The worker-visible facade keeps delivered/outstanding/total summary semantics."""
    database, _path, _failed_id, _delivered_id, source_id = _database(tmp_path)
    with database.session() as session:
        session.add_all(
            [
                UserActorOutboundDelivery(
                    source_id=source_id,
                    target_endpoint_url="https://pending.test/inbox",
                    status="pending",
                ),
                UserActorOutboundDelivery(
                    source_id=source_id,
                    target_endpoint_url="https://sending.test/inbox",
                    status="sending",
                    claim_token="worker-owned",
                ),
                UserActorOutboundDelivery(
                    source_id=source_id,
                    target_endpoint_url="https://ineligible.test/inbox",
                    status="ineligible",
                ),
            ]
        )

    assert database.user_actor_deliveries.source_summary(source_id) == (1, 3, 5)
