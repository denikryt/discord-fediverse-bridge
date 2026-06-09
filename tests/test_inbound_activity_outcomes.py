"""Tests for the stable inbound activity outcome vocabulary and persistence."""

from __future__ import annotations
from support.runtime import build_test_policy_service

from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, inspect, text

from src.db import Database
from src.http_api import _begin_event_processing
from src.inbound_activity_outcomes import InboundActivityOutcome


RECEIPT_STATUSES = {"in_progress", "processed", "skipped", "deferred", "failed"}


def test_outcome_values_are_unique_and_fit_receipt_column() -> None:
    """Every stored outcome must be unique, bounded, and distinct from statuses."""
    values = [outcome.value for outcome in InboundActivityOutcome]

    assert len(values) == len(set(values))
    assert max(map(len, values)) <= 64
    assert set(values).isdisjoint(RECEIPT_STATUSES)


def test_fresh_schema_has_nullable_outcome_column(tmp_path: Path) -> None:
    """A fresh database should create the optional semantic outcome column."""
    database = Database(f"sqlite:///{tmp_path / 'fresh.db'}")
    database.create_all()

    columns = {
        column["name"]: column
        for column in inspect(database.engine).get_columns("activitypub_event_receipts")
    }

    assert columns["outcome"]["nullable"] is True
    assert columns["outcome"]["type"].length == 64


def test_migration_adds_outcome_without_backfilling_legacy_rows(tmp_path: Path) -> None:
    """Migration should preserve legacy receipts and leave outcome unknown."""
    db_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE activitypub_event_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id VARCHAR(128) UNIQUE NOT NULL,
                    event_type VARCHAR(64) NOT NULL,
                    object_ap_id VARCHAR(512) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    detail VARCHAR(1024),
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(text("CREATE TABLE local_community_threads (id INTEGER PRIMARY KEY AUTOINCREMENT)"))
        connection.execute(text("CREATE TABLE local_community_messages (id INTEGER PRIMARY KEY AUTOINCREMENT)"))
        connection.execute(
            text(
                """
                INSERT INTO activitypub_event_receipts (
                    delivery_id, event_type, object_ap_id, status, detail,
                    created_at, updated_at
                ) VALUES (
                    'delivery-1', 'post.created', 'object-1', 'skipped',
                    'legacy detail', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    database = Database(f"sqlite:///{db_path}")
    database.migrate()
    database.migrate()

    with database.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT delivery_id, status, detail, outcome "
                "FROM activitypub_event_receipts"
            )
        ).one()

    assert row == ("delivery-1", "skipped", "legacy detail", None)


def test_repository_updates_status_outcome_and_detail_together(tmp_path: Path) -> None:
    """One repository transition should persist the complete terminal result."""
    database = Database(f"sqlite:///{tmp_path / 'receipt.db'}")
    database.create_all()
    database.event_receipts.create_event_receipt(
        delivery_id="delivery-1",
        event_type="post.created",
        object_ap_id="object-1",
        status="in_progress",
    )

    database.event_receipts.update_event_receipt(
        delivery_id="delivery-1",
        status="skipped",
        outcome=InboundActivityOutcome.IGNORED_BY_BAN,
        detail="actor is banned for this community",
    )
    receipt = database.event_receipts.get_event_receipt("delivery-1")

    assert receipt is not None
    assert receipt.status == "skipped"
    assert receipt.outcome == "ignored_by_ban"
    assert receipt.detail == "actor is banned for this community"


def test_retry_start_clears_stale_terminal_outcome(tmp_path: Path) -> None:
    """Restarting a retry must not expose the previous attempt's outcome."""
    database = Database(f"sqlite:///{tmp_path / 'retry.db'}")
    database.create_all()
    database.event_receipts.create_event_receipt(
        delivery_id="delivery-1",
        event_type="comment.created",
        object_ap_id="object-1",
        status="deferred",
        outcome=InboundActivityOutcome.DEFERRED_MISSING_DEPENDENCY,
        detail="parent post not mapped and fetch failed",
    )

    database.event_receipts.update_event_receipt(
        delivery_id="delivery-1",
        status="in_progress",
        outcome=None,
        detail="retrying deferred delivery",
    )
    receipt = database.event_receipts.get_event_receipt("delivery-1")

    assert receipt is not None
    assert receipt.status == "in_progress"
    assert receipt.outcome is None
    assert receipt.detail == "retrying deferred delivery"


def test_duplicate_legacy_receipt_returns_null_outcome_without_inference(tmp_path: Path) -> None:
    """Duplicate responses must not derive missing outcomes from legacy detail."""
    database = Database(f"sqlite:///{tmp_path / 'legacy-duplicate.db'}")
    database.create_all()
    database.event_receipts.create_event_receipt(
        delivery_id="delivery-1",
        event_type="post.created",
        object_ap_id="object-1",
        status="skipped",
        outcome=None,
        detail="no subscriptions for this community",
    )
    runtime = SimpleNamespace(database=database, bridge_policy_service=build_test_policy_service(database))
    event = SimpleNamespace(delivery_id="delivery-1")

    response = _begin_event_processing(runtime, event)
    receipt = database.event_receipts.get_event_receipt("delivery-1")

    assert response == {
        "status": "duplicate",
        "outcome": None,
        "detail": "no subscriptions for this community",
    }
    assert receipt is not None
    assert receipt.outcome is None
