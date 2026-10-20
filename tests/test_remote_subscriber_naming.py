"""Regression checks for local subscriber registration remote-subscriber naming cleanup."""

from __future__ import annotations

from src.db import Database
from src import models


def test_remote_subscriber_legacy_exports_are_absent() -> None:
    """local subscriber registration should remove old follower-named Python compatibility exports."""
    # local subscriber registration is a naming cleanup boundary.  Keeping the old alias or wrapper
    # methods alive would let future code silently drift back to the obsolete
    # vocabulary that this behavior is meant to eliminate.
    legacy_model_name = "LocalCommunity" + "Follower"
    legacy_method_names = [
        "create_" + "local_community_" + "follower",
        "get_" + "local_community_" + "follower",
        "get_" + "local_community_" + "follower_by_follow_activity_id",
        "update_" + "local_community_" + "follower_acceptance",
        "delete_" + "local_community_" + "follower",
        "list_" + "local_community_" + "followers",
        "list_" + "local_community_" + "followers_for_all",
    ]

    assert not hasattr(models, legacy_model_name)
    for legacy_method_name in legacy_method_names:
        assert not hasattr(Database, legacy_method_name)
