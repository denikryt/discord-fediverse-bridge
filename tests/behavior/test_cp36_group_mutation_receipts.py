"""CP36 red regression for Group mutation receipt durability and fingerprinting."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from src.db import Database
from src.models import ActivityPubEventReceipt


def test_group_receipt_retains_canonical_payload_identity_across_later_mutations(tmp_path: Path) -> None:
    """A reused Activity ID with another body must remain distinguishable after U2."""
    db = Database(f"sqlite:///{tmp_path / 'group-receipt.sqlite'}")
    db.create_all()
    try:
        receipt = db.event_receipts.create_event_receipt(
            delivery_id="https://nodebb.example/activities/announce/update/1",
            event_type="post.updated", object_ap_id="https://person.example/notes/79",
            status="processed", outcome="applied",
        )
        # R02 needs the hash of the *admitted* normalized event, not just its
        # wire ID: the current Group view can have advanced beyond this update.
        with db.session() as session:
            saved = session.scalar(select(ActivityPubEventReceipt).where(
                ActivityPubEventReceipt.id == receipt.id,
            ))
            assert hasattr(saved, "mutation_fingerprint")
            assert saved.mutation_fingerprint is not None
    finally:
        db.close()
