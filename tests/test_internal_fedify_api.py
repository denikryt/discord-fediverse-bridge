"""Behavior tests for the authenticated Fedify Gateway read API."""

from __future__ import annotations
from support.runtime import build_test_policy_service

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.db import Database
from src.http_api import create_http_app


def _runtime(tmp_path: Path) -> tuple[SimpleNamespace, Database]:
    """Create one real SQLite database and the minimal HTTP runtime around it."""
    database = Database(f"sqlite:///{tmp_path / 'internal-fedify.db'}")
    database.create_all()
    settings = SimpleNamespace(
        fedify_shared_secret="internal-secret",
        normalized_fedify_origin="https://bridge.example",
        normalized_public_bridge_base_url="https://bridge.example",
        fedify_origin="https://bridge.example",
        fedify_actor_identifier="bridge",
        federation_allowlist=[],
        registration_session_cookie_name="bridge_registration_session",
        registration_session_ttl_seconds=3600,
    )
    runtime = SimpleNamespace(
        settings=settings,
        database=database,
        registration_service=SimpleNamespace(),
        discord_oauth_client=SimpleNamespace(),
        fedify_gateway=SimpleNamespace(),
        bot=SimpleNamespace(),
        bridge_policy_service=build_test_policy_service(database, settings),
    )
    return runtime, database


def _headers() -> dict[str, str]:
    """Return the internal bearer credential used by Gateway calls."""
    return {"Authorization": "Bearer internal-secret"}


def test_internal_fedify_reads_are_authenticated_and_no_store(tmp_path: Path) -> None:
    """Gateway can resolve persisted actors and keys only with the shared secret."""
    runtime, database = _runtime(tmp_path)
    database.bridge_actor_keys.create(
        actor_url="https://bridge.example/actors/services/bridge",
        key_id="https://bridge.example/actors/services/bridge#main-key",
        key_format="jwk",
        algorithm="RSASSA-PKCS1-v1_5",
        public_key_data='{"kty":"RSA","n":"public"}',
        private_key_data='{"kty":"RSA","d":"private"}',
    )
    database.users.create_user(
        discord_user_id="1",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="user-public",
        private_key_pem="user-private",
    )
    client = TestClient(create_http_app(runtime))

    unauthorized = client.get("/internal/fedify/actors/bridge/key")
    assert unauthorized.status_code == 401

    bridge = client.get("/internal/fedify/actors/bridge/key", headers=_headers())
    assert bridge.status_code == 200
    assert bridge.headers["cache-control"] == "no-store"
    assert bridge.json()["key_format"] == "jwk"

    user = client.get("/internal/fedify/actors/users/alice", headers=_headers())
    assert user.status_code == 200
    assert user.headers["cache-control"] == "no-store"
    assert user.json()["private_key_pem"] == "user-private"

    missing = client.get("/internal/fedify/actors/users/missing", headers=_headers())
    assert missing.status_code == 404


def test_internal_fedify_resolves_community_subscribers_objects_and_mappings(
    tmp_path: Path,
) -> None:
    """The API exposes every persistence read required by Gateway behavior."""
    runtime, database = _runtime(tmp_path)
    community = database.local_communities.create_local_community(
        discord_guild_id=10,
        discord_forum_channel_id=20,
        slug="test",
        display_name="Test",
        summary="Summary",
        created_by_discord_user_id="1",
        actor_url="https://bridge.example/actors/communities/test",
        inbox_url="https://bridge.example/actors/communities/test/inbox",
        outbox_url="https://bridge.example/actors/communities/test/outbox",
        followers_url="https://bridge.example/actors/communities/test/followers",
        public_key_pem="community-public",
        private_key_pem="community-private",
    )
    database.remote_subscribers.create_remote_subscriber(
        local_community_id=community.id,
        remote_actor_id="https://remote.example/u/alice",
        remote_inbox_url="https://remote.example/inbox",
        follow_activity_id="https://remote.example/follows/1",
    )
    database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        community_actor_url=community.actor_url,
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/objects/1",
        kind="post",
        title="Title",
        body_markdown="Body",
        in_reply_to_object_id=None,
        discord_channel_id=20,
        discord_message_id=30,
    )
    database.message_mappings.create_message_mapping(
        source_platform="discord",
        source_id="30",
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/objects/1",
        actor_url="https://bridge.example/actors/users/alice",
        community_actor_url=community.actor_url,
        discord_channel_id=20,
        discord_message_id=30,
    )
    database.remote_subscriptions.create_subscription(
        discord_channel_id=50,
        discord_guild_id=10,
        lemmy_community_actor_id="https://remote.example/c/test",
        lemmy_community_name="test",
        lemmy_community_id=1,
        follow_activity_id="https://bridge.example/follows/1",
        status="accepted",
    )
    client = TestClient(create_http_app(runtime))

    discovery = client.get("/internal/fedify/communities", headers=_headers())
    assert discovery.json()["items"][0]["slug"] == "test"

    subscribers = client.post(
        "/internal/fedify/communities/subscribers",
        headers=_headers(),
        json={"actor_url": community.actor_url},
    )
    assert subscribers.json()["items"][0]["remote_actor_id"].endswith("/alice")

    followers = client.get(
        "/internal/fedify/actors/communities/test/followers?page=1&page_size=40",
        headers=_headers(),
    )
    assert followers.status_code == 200
    assert followers.json() == {
        "followers_url": "https://bridge.example/actors/communities/test/followers",
        "total_items": 1,
        "page": 1,
        "page_size": 40,
        "actor_ids": ["https://remote.example/u/alice"],
    }

    object_response = client.post(
        "/internal/fedify/published-objects/resolve",
        headers=_headers(),
        json={"activity_id": "https://bridge.example/actors/users/alice/activities/create/posts/1"},
    )
    assert object_response.json()["object_id"] == "https://bridge.example/objects/1"

    mapping = client.post(
        "/internal/fedify/message-mappings/resolve",
        headers=_headers(),
        json={"object_id": "https://bridge.example/objects/1"},
    )
    assert mapping.json()["discord_message_id"] == 30

    subscriptions = client.get(
        "/internal/fedify/channel-community-subscriptions", headers=_headers()
    )
    assert subscriptions.json()["items"] == [
        {
            "community_actor_url": "https://remote.example/c/test",
            "follow_activity_id": "https://bridge.example/follows/1",
            "status": "accepted",
        }
    ]

    ambiguous = client.post(
        "/internal/fedify/published-objects/resolve",
        headers=_headers(),
        json={"object_id": "x", "activity_id": "y"},
    )
    assert ambiguous.status_code == 422


def test_internal_user_actor_profile_dto_exposes_public_fields_without_storage_details(
    tmp_path: Path,
) -> None:
    """The gateway DTO carries ordered profile data but no media storage internals."""
    from io import BytesIO

    from PIL import Image

    from src.profile_media import ProfileMediaStore
    from src.user_profiles import UserProfileService

    runtime, database = _runtime(tmp_path)
    user = database.users.create_user(
        discord_user_id="2",
        activitypub_username="bob",
        actor_url="https://bridge.example/actors/users/bob",
        inbox_url="https://bridge.example/actors/users/bob/inbox",
        outbox_url="https://bridge.example/actors/users/bob/outbox",
        followers_url="https://bridge.example/actors/users/bob/followers",
        public_key_pem="user-public",
        private_key_pem="user-private",
    )
    store = ProfileMediaStore(
        tmp_path / "media", max_upload_bytes=1024 * 1024, max_pixels=1_000_000
    )
    store.initialize()
    service = UserProfileService(database, store)
    service.update_identity_text(user.id, "Bob & Co", "Line one\nLine two")
    service.replace_metadata(
        user.id,
        [("Research & Development", "First\nSecond"), ("Site", "https://example.com")],
    )
    image = BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(image, "PNG")
    image.seek(0)
    service.replace_media(user.id, "avatar", image)

    response = TestClient(create_http_app(runtime)).get(
        "/internal/fedify/actors/users/bob", headers=_headers()
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["display_name"] == "Bob & Co"
    assert payload["summary"] == "Line one\nLine two"
    assert payload["fields"] == [
        {"name": "Research & Development", "value": "First\nSecond"},
        {"name": "Site", "value": "https://example.com"},
    ]
    assert payload["avatar"]["url"].startswith("https://bridge.example/media/profile/")
    assert set(payload["avatar"]) == {"url", "content_type", "width", "height"}
    serialized = response.text
    assert "storage_key" not in serialized
    assert "sha256" not in serialized
    assert str(tmp_path) not in serialized


def test_internal_user_actor_profile_defaults_and_cleared_media_are_omitted(
    tmp_path: Path,
) -> None:
    """Legacy users retain username fallback and absent optional profile members."""
    runtime, database = _runtime(tmp_path)
    database.users.create_user(
        discord_user_id="3",
        activitypub_username="carol",
        actor_url="https://bridge.example/actors/users/carol",
        inbox_url="https://bridge.example/actors/users/carol/inbox",
        outbox_url="https://bridge.example/actors/users/carol/outbox",
        followers_url="https://bridge.example/actors/users/carol/followers",
        public_key_pem="user-public",
        private_key_pem="user-private",
    )
    response = TestClient(create_http_app(runtime)).get(
        "/internal/fedify/actors/users/carol", headers=_headers()
    )
    assert response.status_code == 200
    assert response.json() | {} == response.json()
    assert response.json()["display_name"] == "carol"
    assert response.json()["summary"] is None
    assert response.json()["fields"] == []
    assert response.json()["avatar"] is None
    assert response.json()["header"] is None


def test_internal_user_followers_are_newest_first_and_fixed_to_forty(
    tmp_path: Path,
) -> None:
    """Gateway reads receive deterministic newest-first pages with the fixed contract size."""
    runtime, database = _runtime(tmp_path)
    user = database.users.create_user(
        discord_user_id="4",
        activitypub_username="dana",
        actor_url="https://bridge.example/actors/users/dana",
        inbox_url="https://bridge.example/actors/users/dana/inbox",
        outbox_url="https://bridge.example/actors/users/dana/outbox",
        followers_url="https://bridge.example/actors/users/dana/followers",
        public_key_pem="user-public",
        private_key_pem="user-private",
    )
    for index in range(1, 4):
        with database.immediate_session() as session:
            database.user_actor_followers.create_or_refresh(
                session,
                user_id=user.id,
                remote_actor_id=f"https://remote.example/users/{index}",
                remote_inbox_url=f"https://remote.example/users/{index}/inbox",
                remote_shared_inbox_url=None,
                follow_activity_id=f"https://remote.example/activities/follow-{index}",
            )

    client = TestClient(create_http_app(runtime))
    response = client.get(
        "/internal/fedify/actors/users/dana/followers?page=1&page_size=40",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.json()["actor_ids"] == [
        "https://remote.example/users/3",
        "https://remote.example/users/2",
        "https://remote.example/users/1",
    ]
    assert response.json()["page_size"] == 40

    variable_page = client.get(
        "/internal/fedify/actors/users/dana/followers?page=1&page_size=80",
        headers=_headers(),
    )
    assert variable_page.status_code == 400


def test_internal_user_outbox_is_canonical_ordered_and_filtered(tmp_path: Path) -> None:
    """The gateway receives one canonical bounded authored-object timeline."""
    runtime, database = _runtime(tmp_path)
    user = database.users.create_user(
        discord_user_id="900",
        activitypub_username="alice",
        actor_url="https://bridge.example/actors/users/alice",
        inbox_url="https://bridge.example/actors/users/alice/inbox",
        outbox_url="https://bridge.example/actors/users/alice/outbox",
        followers_url="https://bridge.example/actors/users/alice/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    community = database.local_communities.create_local_community(
        discord_guild_id=1,
        discord_forum_channel_id=2,
        slug="stage9",
        display_name="Stage 9",
        summary=None,
        created_by_discord_user_id="900",
        actor_url="https://bridge.example/actors/communities/stage9",
        inbox_url="https://bridge.example/actors/communities/stage9/inbox",
        outbox_url="https://bridge.example/actors/communities/stage9/outbox",
        followers_url="https://bridge.example/actors/communities/stage9/followers",
        public_key_pem="public",
        private_key_pem="private",
    )
    first = database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url=user.actor_url,
        community_actor_url=community.actor_url,
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/1",
        object_id="https://bridge.example/objects/1",
        kind="post",
        title="One",
        body_markdown="one",
        in_reply_to_object_id=None,
        discord_channel_id=2,
        discord_message_id=10,
    )
    second = database.activitypub_objects.create_published_activity_object(
        actor_username="alice",
        actor_url=user.actor_url,
        community_actor_url=community.actor_url,
        activity_id="https://bridge.example/actors/users/alice/activities/create/posts/2",
        object_id="https://bridge.example/objects/2",
        kind="comment",
        title=None,
        body_markdown="two",
        in_reply_to_object_id=first.object_id,
        discord_channel_id=2,
        discord_message_id=11,
    )
    with database.session() as session:
        from src.models import PublishedActivityObject, utcnow

        session.get(PublishedActivityObject, first.id).deleted_at = utcnow()
    client = TestClient(create_http_app(runtime))
    response = client.get(
        "/internal/fedify/actors/users/alice/outbox?page=1&page_size=40",
        headers=_headers(),
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["outbox_url"] == "https://bridge.example/actors/users/alice/outbox"
    assert payload["total_items"] == 1
    assert [item["object_id"] for item in payload["items"]] == [second.object_id]
    assert (
        client.get(
            "/internal/fedify/actors/users/alice/outbox?page=0&page_size=40",
            headers=_headers(),
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/internal/fedify/actors/users/alice/outbox?page=1&page_size=20",
            headers=_headers(),
        ).status_code
        == 422
    )
