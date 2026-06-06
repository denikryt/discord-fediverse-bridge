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
        fedify_origin="https://discrod-bridge.example.com",
        normalized_fedify_origin="https://discrod-bridge.example.com",
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
    """Create one local community with public discrod-bridge.example.com URLs."""
    LocalCommunityService(
        database=database,
        base_url="https://discrod-bridge.example.com",
        keypair_generator=lambda: ("public-key", "private-key"),
    ).create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=100,
        slug="hackers",
        name="Hackers",
        description="A local hackerspace forum.",
        created_by_discord_user_id="123",
    )
    return database.local_communities.get_local_community_by_slug("hackers")


def test_empty_dashboard_state_renders_open_federation(tmp_path: Path) -> None:
    """An empty bridge reports open federation and no public rows."""
    database = _database(tmp_path)
    response = _client(database).get("/dashboard/data")

    assert response.status_code == 200
    payload = response.json()
    assert payload["instance"]["origin"] == "https://discrod-bridge.example.com"
    assert payload["instance"]["bridgeActorUrl"] == "https://discrod-bridge.example.com/actors/bridge"
    assert payload["instance"]["registeredUserCount"] == 0
    assert payload["localCommunities"] == []
    assert payload["bridgeActorFollows"] == []
    assert payload["federation"]["mode"] == "open"
    assert payload["federation"]["allowlist"] == []


def test_local_communities_show_safe_public_metadata_and_counts(tmp_path: Path) -> None:
    """Local communities include public metadata but not private Discord or key fields."""
    database = _database(tmp_path)
    community = _create_local_community(database)
    database.users.create_user(
        discord_user_id="1234567890",
        activitypub_username="alice",
        actor_url="https://discrod-bridge.example.com/users/alice",
        inbox_url="https://discrod-bridge.example.com/users/alice/inbox",
        outbox_url="https://discrod-bridge.example.com/users/alice/outbox",
        followers_url="https://discrod-bridge.example.com/users/alice/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )
    database.users.create_user(
        discord_user_id="9999999999",
        activitypub_username="bob",
        actor_url="https://discrod-bridge.example.com/users/bob",
        inbox_url="https://discrod-bridge.example.com/users/bob/inbox",
        outbox_url="https://discrod-bridge.example.com/users/bob/outbox",
        followers_url="https://discrod-bridge.example.com/users/bob/followers",
        public_key_pem="public-key",
        private_key_pem="private-key",
    )
    database.remote_subscribers.create_remote_subscriber(
        local_community_id=community.id,
        remote_actor_id="https://lemmy.world/u/alice",
        remote_inbox_url="https://lemmy.world/u/alice/inbox",
        follow_activity_id="https://lemmy.world/activities/follow/alice",
    )
    database.remote_subscribers.create_remote_subscriber(
        local_community_id=community.id,
        remote_actor_id="https://beehaw.org/u/bob",
        remote_inbox_url="https://beehaw.org/u/bob/inbox",
        follow_activity_id="https://beehaw.org/activities/follow/bob",
    )
    database.remote_subscribers.create_remote_subscriber(
        local_community_id=community.id,
        remote_actor_id="https://pending.example/u/carol",
        remote_inbox_url="https://pending.example/u/carol/inbox",
        follow_activity_id="https://pending.example/activities/follow/carol",
        status="pending",
    )

    payload = _client(database).get("/dashboard/data").json()

    assert payload["instance"]["registeredUserCount"] == 2
    community_payload = payload["localCommunities"][0]
    assert community_payload["slug"] == "hackers"
    assert community_payload["name"] == "Hackers"
    assert community_payload["description"] == "A local hackerspace forum."
    assert community_payload["relayHandle"] == "!hackers@discrod-bridge.example.com"
    assert community_payload["actorUrl"] == "https://discrod-bridge.example.com/communities/hackers"
    assert community_payload["aliasUrl"] == "https://discrod-bridge.example.com/c/hackers"
    assert community_payload["remoteSubscriberCount"] == 2
    assert community_payload["localSubscriberCount"] == 0
    assert len(community_payload["followers"]) == 2
    assert sorted(follower["actorUrl"] for follower in community_payload["followers"]) == [
        "https://beehaw.org/u/bob",
        "https://lemmy.world/u/alice",
    ]
    serialized = json.dumps(payload)
    assert "discord_guild_id" not in serialized
    assert "private_key_pem" not in serialized
    assert "test-secret" not in serialized


def test_bridge_actor_follows_do_not_change_federation_policy_payload(tmp_path: Path) -> None:
    """Outbound bridge follows stay separate from the public federation policy block."""
    database = _database(tmp_path)
    community = _create_local_community(database)
    database.bridge_actor_follows.create_bridge_actor_follow(
        community_actor_id="https://lemmy.world/c/news",
        follow_activity_id="https://discrod-bridge.example.com/activities/follow/news",
        community_inbox_url="https://lemmy.world/c/news/inbox",
        status="accepted",
    )
    database.remote_subscribers.create_remote_subscriber(
        local_community_id=community.id,
        remote_actor_id="https://beehaw.org/u/bob",
        remote_inbox_url="https://beehaw.org/u/bob/inbox",
        follow_activity_id="https://beehaw.org/activities/follow/bob",
    )

    payload = _client(database).get("/dashboard/data").json()

    assert payload["bridgeActorFollows"][0]["communityActorUrl"] == "https://lemmy.world/c/news"
    assert payload["federation"] == {
        "mode": "open",
        "allowlist": [],
    }


def test_allowlist_mode_is_explicit_and_normalized(tmp_path: Path) -> None:
    """Allowlist entries are normalized as sorted hostnames."""
    database = _database(tmp_path)
    payload = _client(database, allowlist=["lemmy.world", "https://Beehaw.org"]).get("/dashboard/data").json()

    assert payload["federation"]["mode"] == "restricted_allowlist"
    assert payload["federation"]["allowlist"] == ["beehaw.org", "lemmy.world"]


def test_dashboard_html_loads_and_includes_credits(tmp_path: Path) -> None:
    """The browser dashboard shell exposes its JSON endpoint and credits."""
    database = _database(tmp_path)
    response = _client(database).get("/")

    assert response.status_code == 200
    assert "Discord/Fediverse Bridge Instance" in response.text
    assert "/dashboard/data" in response.text
    assert "/dashboard/static/dashboard.css?v=2026-06-06-guild-invite-publication" in response.text
    assert "/dashboard/static/dashboard.js?v=2026-06-06-guild-invite-publication" in response.text
    assert 'data-dashboard-endpoint="/dashboard/data"' in response.text
    assert "Remote follower relays" not in response.text
    assert '<span class="stat-label">Origin</span>' not in response.text
    assert '<span class="stat-label">Bridge actor</span>' not in response.text
    assert "https://nachitima.com" in response.text
    assert "Nachitima" in response.text


def test_dashboard_path_redirects_to_root(tmp_path: Path) -> None:
    """Legacy /dashboard links redirect to the canonical root dashboard."""
    database = _database(tmp_path)
    response = _client(database).get("/dashboard", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/"


def test_dashboard_static_assets_are_served_under_dashboard_prefix(tmp_path: Path) -> None:
    """Dashboard assets are grouped under the dashboard static namespace."""
    database = _database(tmp_path)
    client = _client(database)

    css = client.get("/dashboard/static/dashboard.css")
    script = client.get("/dashboard/static/dashboard.js")

    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert ":root" in css.text
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript") or script.headers[
        "content-type"
    ].startswith("application/javascript")
    assert "data-dashboard-endpoint" in script.text
    assert "Local subscribers" not in script.text
    assert "discordChannelName" in script.text
    assert "actorHandleFromUrl" in script.text
    assert "communityHandleFromUrl" in script.text
    assert "<details open>\n        <summary>Hosted communities" not in script.text
    assert "<details open>\n        <summary>Remote subscriptions" not in script.text
    assert "<details open>\n        <summary>Local subscriptions" not in script.text
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css.text


def test_local_community_host_discord_names_appear_in_dashboard_payload(tmp_path: Path) -> None:
    """A hosted local community shows last-known guild and forum names."""
    database = _database(tmp_path)
    _create_local_community(database)
    database.discord_directory.upsert_guild_snapshot(
        discord_guild_id=10,
        guild_name="Guild 1",
    )
    database.discord_directory.upsert_channel_snapshot(
        discord_channel_id=100,
        discord_guild_id=10,
        channel_name="community-host",
        channel_type="forum",
    )

    payload = _client(database).get("/dashboard/data").json()

    assert payload["localCommunities"][0]["hostDiscord"] == {
        "guildName": "Guild 1",
        "forumChannelName": "community-host",
    }


def test_accepted_remote_subscriptions_are_grouped_by_discord_guild(tmp_path: Path) -> None:
    """Only accepted remote subscriptions appear in the guild placement section."""
    database = _database(tmp_path)
    database.discord_directory.upsert_guild_snapshot(discord_guild_id=55, guild_name="Guild 1")
    database.discord_directory.upsert_channel_snapshot(
        discord_channel_id=501,
        discord_guild_id=55,
        channel_name="lemmy-news",
        channel_type="forum",
    )
    database.discord_directory.upsert_channel_snapshot(
        discord_channel_id=502,
        discord_guild_id=55,
        channel_name="lemmy-tech",
        channel_type="forum",
    )
    database.remote_subscriptions.create_subscription(
        discord_channel_id=501,
        discord_guild_id=55,
        lemmy_community_actor_id="https://lemmy.world/c/news",
        lemmy_community_name="news",
        lemmy_community_id=1,
        community_handle="!news@lemmy.world",
        status="accepted",
    )
    database.remote_subscriptions.create_subscription(
        discord_channel_id=502,
        discord_guild_id=55,
        lemmy_community_actor_id="https://lemmy.world/c/tech",
        lemmy_community_name="tech",
        lemmy_community_id=2,
        community_handle="!tech@lemmy.world",
        status="accepted",
    )
    database.remote_subscriptions.create_subscription(
        discord_channel_id=503,
        discord_guild_id=55,
        lemmy_community_actor_id="https://lemmy.world/c/pending",
        lemmy_community_name="pending",
        lemmy_community_id=3,
        community_handle="!pending@lemmy.world",
        status="pending",
    )

    payload = _client(database).get("/dashboard/data").json()

    guild = payload["discordGuilds"][0]
    assert guild["guildName"] == "Guild 1"
    assert guild["remoteSubscriptions"] == [
        {"forumChannelName": "lemmy-news", "communityHandle": "!news@lemmy.world"},
        {"forumChannelName": "lemmy-tech", "communityHandle": "!tech@lemmy.world"},
    ]
    assert "pending" not in json.dumps(guild)


def test_active_local_subscribers_are_grouped_by_discord_guild(tmp_path: Path) -> None:
    """Only active local subscribers appear with bridge-facing community handles."""
    database = _database(tmp_path)
    community = _create_local_community(database)
    database.discord_directory.upsert_guild_snapshot(discord_guild_id=77, guild_name="Guild 2")
    database.discord_directory.upsert_channel_snapshot(
        discord_channel_id=701,
        discord_guild_id=77,
        channel_name="mirror-forum",
        channel_type="forum",
    )
    database.discord_directory.upsert_channel_snapshot(
        discord_channel_id=702,
        discord_guild_id=77,
        channel_name="inactive-forum",
        channel_type="forum",
    )
    database.local_subscribers.create_local_subscriber(
        local_community_id=community.id,
        discord_guild_id=77,
        discord_channel_id=701,
        initiated_by_discord_user_id="123",
        status="active",
    )
    database.local_subscribers.create_local_subscriber(
        local_community_id=community.id,
        discord_guild_id=77,
        discord_channel_id=702,
        initiated_by_discord_user_id="123",
        status="inactive",
    )

    payload = _client(database).get("/dashboard/data").json()

    guild = next(row for row in payload["discordGuilds"] if row["guildName"] == "Guild 2")
    assert guild["localSubscriptions"] == [
        {
            "forumChannelName": "mirror-forum",
            "communityHandle": "!hackers@discrod-bridge.example.com",
        }
    ]


def test_dashboard_guild_visibility_redacts_numeric_discord_ids(tmp_path: Path) -> None:
    """Guild visibility exposes names but not raw Discord identifiers."""
    database = _database(tmp_path)
    _create_local_community(database)
    database.discord_directory.upsert_guild_snapshot(discord_guild_id=10, guild_name="Guild 1")
    database.discord_directory.upsert_channel_snapshot(
        discord_channel_id=100,
        discord_guild_id=10,
        channel_name="community-host",
        channel_type="forum",
    )

    serialized = json.dumps(_client(database).get("/dashboard/data").json())

    assert "Guild 1" in serialized
    assert "community-host" in serialized
    assert "discord_guild_id" not in serialized
    assert "discord_channel_id" not in serialized
    assert "private_key_pem" not in serialized
    assert "fedify_shared_secret" not in serialized


def test_missing_discord_snapshots_render_fallback_labels(tmp_path: Path) -> None:
    """Rows without cached Discord names still produce stable public labels."""
    database = _database(tmp_path)
    community = _create_local_community(database)
    database.local_subscribers.create_local_subscriber(
        local_community_id=community.id,
        discord_guild_id=None,
        discord_channel_id=888,
        initiated_by_discord_user_id="123",
        status="active",
    )

    payload = _client(database).get("/dashboard/data").json()

    assert payload["localCommunities"][0]["hostDiscord"] == {
        "guildName": "Unknown guild",
        "forumChannelName": "Unknown forum channel",
    }
    assert any(row["guildName"] == "Unknown guild" for row in payload["discordGuilds"])
