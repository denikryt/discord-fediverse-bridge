"""Stable outbound Undo identity and strict legacy-provenance tests."""
from __future__ import annotations
import hashlib
from src.activitypub_identity_urls import derive_service_undo_follow_activity_id


def test_typed_service_follow_derives_the_same_undo_id_after_restart() -> None:
    """Two independent callers use the same immutable Follow ID and digest."""
    follow='https://bridge.example/actors/services/bridge/activities/follow/fixed'
    expected=(
        'https://bridge.example/actors/services/bridge/activities/undo/follows/'
        + hashlib.sha256(follow.encode()).hexdigest()
    )
    assert derive_service_undo_follow_activity_id(follow)==expected
    assert derive_service_undo_follow_activity_id(follow)==expected
    assert derive_service_undo_follow_activity_id(follow+'-new') != expected


def test_untyped_legacy_or_untrusted_follow_id_cannot_supply_new_undo_actor() -> None:
    """An unrelated URL has no verifiable Service root to construct Undo from."""
    assert derive_service_undo_follow_activity_id('https://bridge.example/activities/follow/old') is None
    assert derive_service_undo_follow_activity_id('https://elsewhere.example/follow?id=old') is None
    assert derive_service_undo_follow_activity_id('http://bridge.example/actors/services/a/activities/follow/1') is None
