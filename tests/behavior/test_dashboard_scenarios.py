"""Behavior scenarios for the public bridge dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.db import Database
from src.http_api import create_http_app
from src.local_communities.service import LocalCommunityService


def _database(tmp_path: Path) -> Database:
    """Create one real SQLite repository for dashboard behavior tests."""
    database = Database(f"sqlite:///{tmp_path / 'dashboard.db'}")
    database.create_all()
    return database


def _runtime(database: Database, *, allowlist: list[str] | None = None) -> SimpleNamespace:
    """Build a minimal runtime carrying dashboard-owned dependencies."""
    settings = SimpleNamespace(
        fedify_origin="https://bot.example.com",
        normalized_fedify_origin="https://bot.example.com",
        fedify_actor_identifier="bridge",
        federation_allowlist=allowlist or [],
        fedify_shared_secret="test-secret",
        registration_session_cookie_name="bridge_registration_session",
        registration_session_ttl_seconds=3600,
    )
    return SimpleNamespace(
        settings=settings,
        database=database,
        registration_service=SimpleNamespace(),
        discord_oauth_client=SimpleNamespace(),
        fedify_gateway=SimpleNamespace(),
        bot=SimpleNamespace(),
    )


def _client(database: Database, *, allowlist: list[str] | None = None) -> TestClient:
    """Create one dashboard route client."""
    return TestClient(create_http_app(_runtime(database, allowlist=allowlist)))


def _create_local_community(database: Database) -> object:
    """Create one local community with public bot.example.com URLs."""
    LocalCommunityService(
        database=database,
        base_url="https://bot.example.com",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="hackers",
        name="Hackers",
        description="A local hackerspace forum.",
    )
    return database.get_local_community_by_slug("hackers")


def test_empty_dashboard_state_renders_open_federation(tmp_path: Path) -> None:
    """An empty bridge reports open federation and no public rows."""
    database = _database(tmp_path)
    response = _client(database).get("/dashboard/data")

    assert response.status_code == 200
    payload = response.json()
    assert payload["instance"]["origin"] == "https://bot.example.com"
    assert payload["instance"]["bridgeActorUrl"] == "https://bot.example.com/actors/bridge"
    assert payload["localCommunities"] == []
    assert payload["bridgeActorFollows"] == []
    assert payload["federation"]["mode"] == "open"
    assert payload["federation"]["allowlist"] == []


def test_local_communities_show_safe_public_metadata_and_counts(tmp_path: Path) -> None:
    """Local communities include public metadata but not private Discord or key fields."""
    database = _database(tmp_path)
    community = _create_local_community(database)
    database.create_local_community_follower(
        local_community_id=community.id,
        remote_actor_id="https://lemmy.world/u/alice",
        remote_inbox_url="https://lemmy.world/u/alice/inbox",
        follow_activity_id="https://lemmy.world/activities/follow/alice",
    )
    database.create_local_community_follower(
        local_community_id=community.id,
        remote_actor_id="https://beehaw.org/u/bob",
        remote_inbox_url="https://beehaw.org/u/bob/inbox",
        follow_activity_id="https://beehaw.org/activities/follow/bob",
    )
    database.create_local_community_follower(
        local_community_id=community.id,
        remote_actor_id="https://pending.example/u/carol",
        remote_inbox_url="https://pending.example/u/carol/inbox",
        follow_activity_id="https://pending.example/activities/follow/carol",
        status="pending",
    )

    payload = _client(database).get("/dashboard/data").json()

    community_payload = payload["localCommunities"][0]
    assert community_payload["slug"] == "hackers"
    assert community_payload["name"] == "Hackers"
    assert community_payload["description"] == "A local hackerspace forum."
    assert community_payload["actorUrl"] == "https://bot.example.com/communities/hackers"
    assert community_payload["aliasUrl"] == "https://bot.example.com/c/hackers"
    assert community_payload["subscriberCount"] == 2
    assert len(community_payload["followers"]) == 2
    serialized = json.dumps(payload)
    assert "discord_guild_id" not in serialized
    assert "private_key_pem" not in serialized
    assert "test-secret" not in serialized


def test_bridge_actor_follows_are_separate_from_connected_follower_instances(tmp_path: Path) -> None:
    """Outbound bridge follows do not define connected local-community instances."""
    database = _database(tmp_path)
    community = _create_local_community(database)
    database.create_bridge_actor_follow(
        community_actor_id="https://lemmy.world/c/news",
        follow_activity_id="https://bot.example.com/activities/follow/news",
        community_inbox_url="https://lemmy.world/c/news/inbox",
        status="accepted",
    )
    database.create_local_community_follower(
        local_community_id=community.id,
        remote_actor_id="https://beehaw.org/u/bob",
        remote_inbox_url="https://beehaw.org/u/bob/inbox",
        follow_activity_id="https://beehaw.org/activities/follow/bob",
    )

    payload = _client(database).get("/dashboard/data").json()

    assert payload["bridgeActorFollows"][0]["communityActorUrl"] == "https://lemmy.world/c/news"
    assert payload["federation"]["connectedFollowerInstances"] == ["beehaw.org"]


def test_allowlist_mode_is_explicit_and_normalized(tmp_path: Path) -> None:
    """Allowlist entries are normalized as sorted hostnames."""
    database = _database(tmp_path)
    payload = _client(database, allowlist=["lemmy.world", "https://Beehaw.org"]).get("/dashboard/data").json()

    assert payload["federation"]["mode"] == "restricted_allowlist"
    assert payload["federation"]["allowlist"] == ["beehaw.org", "lemmy.world"]


def test_dashboard_html_loads_and_includes_credits(tmp_path: Path) -> None:
    """The browser dashboard shell exposes its JSON endpoint and credits."""
    database = _database(tmp_path)
    response = _client(database).get("/dashboard")

    assert response.status_code == 200
    assert "Discord/Fediverse Bridge Instance" in response.text
    assert "/dashboard/data" in response.text
    assert "https://nachitima.com" in response.text
    assert "Nachitima" in response.text
