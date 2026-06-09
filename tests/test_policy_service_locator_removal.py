"""Regression contracts for explicit runtime policy-service ownership."""

from __future__ import annotations
from support.runtime import build_test_policy_service

from pathlib import Path
from types import SimpleNamespace

import pytest

import src.bridge_policy as bridge_policy
from src.activitypub_handlers import dispatch_activitypub_event
from src.activitypub_models import ActivityPubEvent
from src.dashboard import build_dashboard_payload
from src.db import Database


def _database(tmp_path: Path) -> Database:
    """Create one real policy repository for direct-ownership regressions."""
    database = Database(f"sqlite:///{tmp_path / 'policy-owner.db'}")
    database.create_all()
    return database


def _settings() -> SimpleNamespace:
    """Build the settings fields used by policy and dashboard evaluation."""
    return SimpleNamespace(
        federation_allowlist=["allowed.example"],
        federation_blocklist=[],
        discord_guild_allowlist=[],
        discord_guild_blocklist=[],
        bridge_super_admin_user_ids=[],
        fedify_origin="https://bridge.example",
        normalized_fedify_origin="https://bridge.example",
        fedify_actor_identifier="bridge",
    )


def _denied_event() -> ActivityPubEvent:
    """Build an inbound event denied before downstream runtime handlers run."""
    return ActivityPubEvent.model_validate(
        {
            "event_type": "post.created",
            "delivery_id": "https://denied.example/activities/create/1",
            "occurred_at": "2026-06-09T00:00:00Z",
            "community_actor_id": "https://denied.example/c/news",
            "actor_id": "https://denied.example/u/alice",
            "object": {
                "ap_id": "https://denied.example/post/1",
                "kind": "post",
                "lemmy_id": 1,
                "post_ap_id": None,
                "post_lemmy_id": None,
                "parent_ap_id": None,
                "title": "Denied",
                "body_markdown": "Denied",
                "url": "https://denied.example/post/1",
                "published_at": "2026-06-09T00:00:00Z",
                "author_name": "alice",
            },
        }
    )


def test_policy_locator_helpers_are_removed() -> None:
    """The policy module must not expose dynamic service discovery helpers."""
    assert not hasattr(bridge_policy, "bridge_policy_service_for")
    assert not hasattr(bridge_policy, "runtime_bridge_policy_service")


@pytest.mark.asyncio
async def test_activitypub_runtime_does_not_reconstruct_missing_policy_service(
    tmp_path: Path,
) -> None:
    """Inbound admission fails at the explicit owner boundary when wiring is incomplete."""
    runtime = SimpleNamespace(settings=_settings(), database=_database(tmp_path))

    with pytest.raises(AttributeError):
        await dispatch_activitypub_event(_denied_event(), runtime)


def test_dashboard_runtime_does_not_reconstruct_missing_policy_service(
    tmp_path: Path,
) -> None:
    """Dashboard aggregation requires the same explicit service as production runtime."""
    runtime = SimpleNamespace(settings=_settings(), database=_database(tmp_path))

    with pytest.raises(AttributeError):
        build_dashboard_payload(runtime)
